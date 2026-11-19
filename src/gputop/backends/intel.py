"""Intel GPU backend (i915 / xe), read-only, no root required.

Data sources (every read is wrapped; anything missing or unreadable is
reported as ``None`` so the UI hides it):

==================  =========================================================
metric              source
==================  =========================================================
devices             ``/sys/class/drm/cardN`` with ``device/vendor == 0x8086``
frequency           ``gt_act_freq_mhz`` (actual), ``gt_cur_freq_mhz``
                    (requested, fallback), ``gt_max_freq_mhz``; also the
                    per-GT ``gt/gt0/rps_*`` and xe ``tile0/gt0/freq0`` files
utilisation         i915 perf PMU ``<engine>-busy`` counters (same source as
                    ``intel_gpu_top``), sampled as deltas
power               RAPL ``/sys/class/powercap``: the GPU domain (``uncore``
                    / ``gpu``) if present, else the CPU package; discrete
                    cards use their own hwmon ``energy1_input``
temperature         hwmon: coretemp "Package id 0" for integrated GPUs, the
                    card's own hwmon for discrete ones
PCIe link           ``device/current_link_{speed,width}`` (discrete only)
throttle reasons    ``gt/gt0/throttle_reason_*`` (newer kernels)
processes           ``/proc/PID/fdinfo`` ``drm-*`` keys (kernel >= 5.19)
==================  =========================================================

Integrated GPUs share system memory, so no VRAM numbers are reported for them.

Permissions: system-wide perf events need ``kernel.perf_event_paranoid <= 0``
or ``CAP_PERFMON``; RAPL energy is root-only on most distros since 2020. When
the PMU is not permitted the sample carries a one-line hint instead.
Nothing here writes to sysfs, loads modules or changes GPU state.
"""

from __future__ import annotations

import ctypes
import errno
import logging
import os
import platform
import pwd
import re
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .base import BackendError, DeviceLostError, GPUBackend, GPUInfo, GPUProcess, GPUSample

log = logging.getLogger(__name__)

INTEL_VENDOR = "0x8086"
_CARD_RE = re.compile(r"^card\d+$")
_FDINFO_SCAN_INTERVAL = 5.0  # seconds between full /proc scans for DRM clients

# i915 PMU engine classes -> friendly names (instance > 0 gets a suffix).
_ENGINE_NAMES = {"rcs": "render", "bcs": "copy", "vcs": "video", "vecs": "enhance", "ccs": "compute"}
# fdinfo engine keys (drm-engine-<name>) that count as "compute" work.
_COMPUTE_ENGINES = {"compute", "ccs"}

# throttle_reason_* file -> gputop throttle reason (see base.THROTTLE_LABELS)
_THROTTLE_FILES = {
    "pl1": "sw_power",
    "pl2": "sw_power",
    "pl4": "sw_power",
    "thermal": "sw_thermal",
    "ratl": "sw_thermal",
    "vr_thermalert": "sw_thermal",
    "prochot": "hw_thermal",
    "vr_tdc": "hw_slowdown",
}

_LINK_GEN = {"2.5": 1, "5.0": 2, "8.0": 3, "16.0": 4, "32.0": 5, "64.0": 6}


# -- tiny read helpers ---------------------------------------------------------


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, ValueError):
        return None


def _read_int(path: Path) -> int | None:
    text = _read(path)
    if text is None:
        return None
    try:
        return int(text.split()[0], 0)
    except (ValueError, IndexError):
        return None


def _first_int(paths: list[Path]) -> int | None:
    for p in paths:
        v = _read_int(p)
        if v is not None:
            return v
    return None


# -- perf PMU ------------------------------------------------------------------


class Counter(Protocol):
    def read(self) -> tuple[int, int]:
        """(counter value, time enabled in ns)."""

    def close(self) -> None: ...


CounterOpener = Callable[[int, int, int], Counter]
"""``opener(pmu_type, config, cpu)`` -> Counter; raises OSError on failure."""


class _PerfEventAttr(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("size", ctypes.c_uint32),
        ("config", ctypes.c_uint64),
        ("sample_period", ctypes.c_uint64),
        ("sample_type", ctypes.c_uint64),
        ("read_format", ctypes.c_uint64),
        ("flags", ctypes.c_uint64),
        ("wakeup_events", ctypes.c_uint32),
        ("bp_type", ctypes.c_uint32),
        ("config1", ctypes.c_uint64),
        ("config2", ctypes.c_uint64),
    ]


_PERF_FORMAT_TOTAL_TIME_ENABLED = 1
_PERF_FLAG_FD_CLOEXEC = 8
_NR_PERF_EVENT_OPEN = {"x86_64": 298, "amd64": 298, "i386": 336, "i686": 336, "aarch64": 241}


class PerfCounter:
    """A counting (non-sampling) perf event opened with perf_event_open(2)."""

    _libc = None

    def __init__(self, pmu_type: int, config: int, cpu: int) -> None:
        nr = _NR_PERF_EVENT_OPEN.get(platform.machine().lower())
        if nr is None:
            raise OSError(errno.ENOSYS, "perf_event_open not supported on this architecture")
        if PerfCounter._libc is None:
            PerfCounter._libc = ctypes.CDLL(None, use_errno=True)
        attr = _PerfEventAttr()
        attr.type = pmu_type
        attr.size = ctypes.sizeof(_PerfEventAttr)
        attr.config = config
        attr.read_format = _PERF_FORMAT_TOTAL_TIME_ENABLED
        fd = PerfCounter._libc.syscall(
            ctypes.c_long(nr), ctypes.byref(attr), ctypes.c_long(-1), ctypes.c_long(cpu),
            ctypes.c_long(-1), ctypes.c_ulong(_PERF_FLAG_FD_CLOEXEC),
        )
        if fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        self.fd = fd

    def read(self) -> tuple[int, int]:
        data = os.read(self.fd, 16)
        value, enabled = struct.unpack("QQ", data)
        return value, enabled

    def close(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            finally:
                self.fd = -1


def _engine_label(event: str) -> str:
    """'rcs0' -> 'render', 'vcs1' -> 'video1'."""
    m = re.fullmatch(r"([a-z]+)(\d+)", event)
    if not m:
        return event
    cls, inst = m.group(1), int(m.group(2))
    name = _ENGINE_NAMES.get(cls, cls)
    return name if inst == 0 else f"{name}{inst}"


class EngineBusyReader:
    """Per-engine busy % from i915 PMU ``*-busy`` counters (deltas)."""

    def __init__(self, counters: dict[str, Counter]) -> None:
        self.counters = counters
        self._prev: dict[str, tuple[int, int]] = {}
        for name, c in counters.items():
            try:
                self._prev[name] = c.read()
            except OSError:
                pass

    def sample(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for name, c in self.counters.items():
            try:
                value, enabled = c.read()
            except OSError:
                continue
            prev = self._prev.get(name)
            self._prev[name] = (value, enabled)
            if prev is None:
                continue
            dt = enabled - prev[1]
            if dt <= 0:
                continue
            out[name] = max(0.0, min(100.0, 100.0 * (value - prev[0]) / dt))
        return out

    def close(self) -> None:
        for c in self.counters.values():
            try:
                c.close()
            except OSError:
                pass
        self.counters = {}


# -- per-card state --------------------------------------------------------------


@dataclass
class _EnergySource:
    path: Path
    unit_to_joule: float  # energy_uj / energy1_input are both µJ
    max_range: int | None
    label: str
    limit_w: float | None = None
    prev: tuple[int, float] | None = None


@dataclass
class _Card:
    index: int
    sys: Path  # /sys/class/drm/cardN
    dev: Path  # /sys/class/drm/cardN/device
    pci: str
    device_id: int
    driver: str
    integrated: bool
    freq_act: list[Path] = field(default_factory=list)
    freq_cur: list[Path] = field(default_factory=list)
    freq_max: list[Path] = field(default_factory=list)
    temp: Path | None = None
    energy: _EnergySource | None = None
    engines: EngineBusyReader | None = None
    notes: tuple[str, ...] = ()
    throttle_dir: Path | None = None
    ready: bool = False  # per-card setup done (only ever for awake cards)


@dataclass
class _Client:
    pid: int
    fdinfo: Path
    pci: str
    is_card_node: bool


class IntelBackend(GPUBackend):
    name = "intel"

    def __init__(
        self,
        root: str | os.PathLike[str] = "/",
        counter_opener: CounterOpener | None = None,
        clock: Callable[[], float] = time.monotonic,
        scan_interval: float = _FDINFO_SCAN_INTERVAL,
    ) -> None:
        """
        Args:
            root: filesystem root (tests point this at a fake sysfs/procfs tree).
            counter_opener: opens perf counters; defaults to perf_event_open(2).
            clock: monotonic clock, injectable for tests.
            scan_interval: seconds between full /proc scans for DRM clients.
        """
        self.root = Path(root)
        self._open_counter: CounterOpener = counter_opener or PerfCounter
        self._clock = clock
        self._scan_interval = scan_interval
        self._cards: list[_Card] = []
        self._initialised = False
        # fdinfo state
        self._clients: list[_Client] = []
        self._last_scan: float | None = None
        self._fdinfo_seen: set[str] = set()  # PCI addresses with drm-* fdinfo
        self._proc_prev: dict[tuple[int, str], tuple[dict[str, int], float]] = {}
        self._owners: dict[int, tuple[str, str, str]] = {}  # pid -> (user, command, comm)

    # -- paths ----------------------------------------------------------------

    def _p(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    # -- lifecycle ------------------------------------------------------------

    def init(self) -> None:
        drm = self._p("/sys/class/drm")
        try:
            names = sorted(os.listdir(drm), key=lambda n: (len(n), n))
        except OSError as exc:
            raise BackendError(f"cannot read {drm}: {exc.strerror}") from exc
        cards = []
        for name in names:
            if not _CARD_RE.match(name):
                continue
            card = self._probe_card(drm / name, len(cards))
            if card is not None:
                cards.append(card)
        if not cards:
            raise BackendError("no Intel GPU found under /sys/class/drm")
        self._cards = cards
        self._initialised = True
        self._scan_clients(force=True)

    def shutdown(self) -> None:
        for card in self._cards:
            if card.engines is not None:
                card.engines.close()
                card.engines = None
            card.ready = False
        self._initialised = False

    # -- discovery ------------------------------------------------------------

    def _probe_card(self, sys_dir: Path, index: int) -> _Card | None:
        dev = sys_dir / "device"
        if _read(dev / "vendor") != INTEL_VENDOR:
            return None
        cls = _read(dev / "class") or ""
        if cls and not cls.startswith("0x03"):  # display controllers only
            return None
        try:
            pci = os.path.basename(os.path.realpath(dev))
        except OSError:
            pci = ""
        try:
            driver = os.path.basename(os.path.realpath(dev / "driver")) if (dev / "driver").exists() else ""
        except OSError:
            driver = ""
        card = _Card(
            index=index,
            sys=sys_dir,
            dev=dev,
            pci=pci,
            device_id=_read_int(dev / "device") or 0,
            driver=driver or "i915",
            # Intel integrated GPUs always sit at 00:02.0 on the root bus.
            integrated=pci.endswith(":00:02.0") or pci == "0000:00:02.0",
        )
        gt = sys_dir / "gt" / "gt0"
        xe = dev / "tile0" / "gt0" / "freq0"
        card.freq_act = [sys_dir / "gt_act_freq_mhz", gt / "rps_act_freq_mhz", xe / "act_freq"]
        card.freq_cur = [sys_dir / "gt_cur_freq_mhz", gt / "rps_cur_freq_mhz", xe / "cur_freq"]
        card.freq_max = [sys_dir / "gt_max_freq_mhz", gt / "rps_max_freq_mhz", xe / "max_freq"]
        if (gt / "throttle_reason_status").exists():
            card.throttle_dir = gt
        return card

    def _ensure(self, card: _Card) -> None:
        """Per-card setup (sensors, energy counters, PMU). Deferred until the
        card is actually queried, which only happens while it is awake: a
        hwmon read or perf event on a runtime-suspended GPU can wake it."""
        if card.ready:
            return
        card.temp = self._find_temp(card)
        card.energy = self._find_energy(card)
        notes = []
        card.engines, note = self._open_engines(card)
        if note:
            notes.append(note)
        if card.energy is not None and card.energy.label == "package":
            notes.append("power shown is the whole CPU package (no GPU RAPL domain)")
        card.notes = tuple(notes)
        card.ready = True

    def _find_temp(self, card: _Card) -> Path | None:
        # Discrete cards: their own hwmon.
        for hw in sorted((card.dev / "hwmon").glob("hwmon*")):
            for t in sorted(hw.glob("temp*_input")):
                if _read_int(t) is not None:
                    return t
        if not card.integrated:
            return None
        # Integrated: CPU package temperature from coretemp.
        for hw in sorted(self._p("/sys/class/hwmon").glob("hwmon*")):
            if _read(hw / "name") != "coretemp":
                continue
            fallback = None
            for label in sorted(hw.glob("temp*_label")):
                inp = label.with_name(label.name.replace("_label", "_input"))
                if _read(label) == "Package id 0" and _read_int(inp) is not None:
                    return inp
                if fallback is None and _read_int(inp) is not None:
                    fallback = inp
            if fallback is not None:
                return fallback
        return None

    def _find_energy(self, card: _Card) -> _EnergySource | None:
        if not card.integrated:
            for hw in sorted((card.dev / "hwmon").glob("hwmon*")):
                e = hw / "energy1_input"
                if _read_int(e) is not None:
                    limit = _read_int(hw / "power1_max")
                    return _EnergySource(e, 1e-6, None, "card", limit / 1e6 if limit else None)
            return None
        zones = sorted(self._p("/sys/class/powercap").glob("intel-rapl:*"))
        gpu = [z for z in zones if _read(z / "name") in ("uncore", "gpu")]
        pkg = [z for z in zones if (_read(z / "name") or "").startswith("package")]
        for zone, label in [(z, "gpu") for z in gpu] + [(z, "package") for z in pkg[:1]]:
            energy = zone / "energy_uj"
            if _read_int(energy) is None:  # root-only on most systems: hide
                continue
            limit = _read_int(zone / "constraint_0_power_limit_uw")
            return _EnergySource(
                energy, 1e-6, _read_int(zone / "max_energy_range_uj"), label,
                limit / 1e6 if limit else None,
            )
        return None

    def _pmu_dir(self, card: _Card) -> Path | None:
        base = self._p("/sys/bus/event_source/devices")
        candidates = []
        if card.pci:
            candidates.append(base / ("i915_" + card.pci.replace(":", "_")))
        if card.integrated:
            candidates.append(base / "i915")
        for c in candidates:
            if (c / "type").exists():
                return c
        return None

    def _open_engines(self, card: _Card) -> tuple[EngineBusyReader | None, str | None]:
        if card.driver != "i915":
            return None, None  # xe uses a different PMU; not supported yet
        pmu = self._pmu_dir(card)
        if pmu is None:
            return None, None
        pmu_type = _read_int(pmu / "type")
        cpumask = _read(pmu / "cpumask") or "0"
        try:
            cpu = int(re.split(r"[-,]", cpumask)[0])
        except ValueError:
            cpu = 0
        if pmu_type is None:
            return None, None
        events: dict[str, int] = {}
        try:
            names = sorted(os.listdir(pmu / "events"))
        except OSError:
            return None, None
        for name in names:
            if not name.endswith("-busy"):
                continue
            m = re.search(r"config=(0x[0-9a-fA-F]+|\d+)", _read(pmu / "events" / name) or "")
            if m:
                events[name[: -len("-busy")]] = int(m.group(1), 0)
        if not events:
            return None, None
        counters: dict[str, Counter] = {}
        for engine, config in events.items():
            try:
                counters[engine] = self._open_counter(pmu_type, config, cpu)
            except OSError as exc:
                for c in counters.values():
                    c.close()
                if exc.errno in (errno.EACCES, errno.EPERM):
                    paranoid = _read(self._p("/proc/sys/kernel/perf_event_paranoid"))
                    now = f" (now {paranoid})" if paranoid is not None else ""
                    return None, f"util hidden: needs kernel.perf_event_paranoid=0{now} or CAP_PERFMON"
                log.debug("cannot open i915 PMU event %s: %s", engine, exc)
                return None, None
        return EngineBusyReader(counters), None

    def _name(self, device_id: int) -> str:
        for path in ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids", "/usr/share/pci.ids"):
            name = self._lookup_pci_ids(self._p(path), device_id)
            if name:
                return f"Intel {name}"
        return f"Intel GPU [8086:{device_id:04x}]"

    @staticmethod
    def _lookup_pci_ids(path: Path, device_id: int) -> str | None:
        want = f"\t{device_id:04x}  "
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                in_vendor = False
                for line in fh:
                    if line.startswith("8086  "):
                        in_vendor = True
                        continue
                    if in_vendor:
                        if line and not line.startswith("\t"):
                            if line.startswith("#"):
                                continue
                            return None
                        if line.startswith(want):
                            return line[len(want):].strip()
        except OSError:
            return None
        return None

    def get_devices(self) -> list[GPUInfo]:
        if not self._initialised:
            self.init()
        return self._infos(self._cards)

    def get_devices_for(self, addresses) -> list[GPUInfo]:
        """Only the cards at ``addresses``; other cards are not touched."""
        if not self._initialised:
            self.init()
        want = set(addresses)
        return self._infos([c for c in self._cards if c.pci in want])

    def _infos(self, cards: list[_Card]) -> list[GPUInfo]:
        release = _read(self._p("/proc/sys/kernel/osrelease")) or platform.release()
        out = []
        for card in cards:
            self._ensure(card)
            gen = width = None
            if not card.integrated:
                gen = self._link_gen(_read(card.dev / "max_link_speed"))
                width = _read_int(card.dev / "max_link_width")
            out.append(
                GPUInfo(
                    index=card.index,
                    name=self._name(card.device_id),
                    uuid=card.pci,
                    driver_version=f"{card.driver} {release}",
                    vendor="Intel",
                    memory_total=None,  # integrated: shared memory; discrete: not exposed
                    pcie_gen_max=gen,
                    pcie_width_max=width,
                    clock_graphics_max=_first_int(card.freq_max),
                    power_limit_max=card.energy.limit_w if card.energy else None,
                    pci_address=card.pci,
                )
            )
        return out

    def _card(self, device: GPUInfo) -> _Card:
        for card in self._cards:
            if card.index == device.index:
                if not card.sys.exists() or not card.dev.exists():
                    raise DeviceLostError(device.index, f"GPU {device.index} ({card.pci}) disappeared")
                return card
        raise DeviceLostError(device.index)

    # -- sampling -------------------------------------------------------------

    @staticmethod
    def _link_gen(speed: str | None) -> int | None:
        if not speed:
            return None
        return _LINK_GEN.get(speed.split()[0])

    def sample(self, device: GPUInfo) -> GPUSample:
        card = self._card(device)
        self._ensure(card)
        s = GPUSample(index=device.index, notes=card.notes)

        act = _first_int(card.freq_act)
        s.clock_graphics = act if act is not None else _first_int(card.freq_cur)

        if card.engines is not None:
            busy = card.engines.sample()
            if busy:
                s.engine_util = {_engine_label(k): round(v, 1) for k, v in sorted(busy.items())}
                main = [v for k, v in busy.items() if k.startswith(("rcs", "ccs"))]
                s.util_gpu = round(max(main) if main else max(busy.values()), 1)

        if card.temp is not None:
            t = _read_int(card.temp)
            s.temp_core = t / 1000.0 if t is not None else None

        if card.energy is not None:
            s.power_draw = self._power(card.energy)
            s.power_limit = card.energy.limit_w

        if not card.integrated:
            s.pcie_gen = self._link_gen(_read(card.dev / "current_link_speed"))
            s.pcie_width = _read_int(card.dev / "current_link_width")

        if card.throttle_dir is not None:
            reasons = []
            for name, reason in _THROTTLE_FILES.items():
                if _read_int(card.throttle_dir / f"throttle_reason_{name}") and reason not in reasons:
                    reasons.append(reason)
            s.throttle_reasons = tuple(reasons)
        return s

    def _power(self, src: _EnergySource) -> float | None:
        value = _read_int(src.path)
        now = self._clock()
        if value is None:
            return None
        prev, src.prev = src.prev, (value, now)
        if prev is None:
            return None
        delta = value - prev[0]
        if delta < 0:  # counter wrapped
            if not src.max_range:
                return None
            delta += src.max_range + 1
        dt = now - prev[1]
        if dt <= 0:
            return None
        return round(delta * src.unit_to_joule / dt, 1)

    # -- processes (fdinfo) ---------------------------------------------------

    def supports_processes(self, device: GPUInfo) -> bool:
        for card in self._cards:
            if card.index == device.index:
                return card.pci in self._fdinfo_seen
        return False

    def _scan_clients(self, force: bool = False) -> None:
        """Find every readable DRM file descriptor under /proc (rate limited)."""
        now = self._clock()
        if not force and self._last_scan is not None and now - self._last_scan < self._scan_interval:
            return
        self._last_scan = now
        proc = self._p("/proc")
        known = {c.pci for c in self._cards}
        clients: list[_Client] = []
        try:
            pids = [int(p) for p in os.listdir(proc) if p.isdigit()]
        except OSError:
            self._clients = []
            return
        for pid in pids:
            fd_dir = proc / str(pid) / "fd"
            try:
                fds = sorted(os.listdir(fd_dir), key=lambda f: int(f) if f.isdigit() else -1)
            except OSError:  # other users' processes, or exited
                continue
            for fd in fds:
                try:
                    target = os.readlink(fd_dir / fd)
                except OSError:
                    continue
                if not target.startswith("/dev/dri/"):
                    continue
                info = proc / str(pid) / "fdinfo" / fd
                keys = self._parse_fdinfo(info)
                pci = keys.get("drm-pdev") if keys else None
                if not keys or "drm-driver" not in keys:
                    continue
                if pci in known:
                    self._fdinfo_seen.add(pci)
                    clients.append(_Client(pid, info, pci, "/card" in target))
        self._clients = clients
        alive = {c.pid for c in clients}
        for pid in [p for p in self._owners if p not in alive]:
            del self._owners[pid]
        for key in [k for k in self._proc_prev if k[0] not in alive]:
            del self._proc_prev[key]

    @staticmethod
    def _parse_fdinfo(path: Path) -> dict[str, str] | None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        out = {}
        for line in text.splitlines():
            if line.startswith("drm-"):
                key, _, value = line.partition(":")
                out[key.strip()] = value.strip()
        return out

    @staticmethod
    def _parse_size(value: str) -> int | None:
        """'310820 KiB' -> bytes (fdinfo memory keys)."""
        parts = value.split()
        try:
            n = int(parts[0])
        except (ValueError, IndexError):
            return None
        unit = parts[1] if len(parts) > 1 else ""
        return n * {"": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}.get(unit, 1)

    def _owner(self, pid: int) -> tuple[str, str, str]:
        cached = self._owners.get(pid)
        if cached is not None:
            return cached
        base = self._p(f"/proc/{pid}")
        user = "?"
        try:
            uid = os.stat(base).st_uid
            try:
                user = pwd.getpwuid(uid).pw_name
            except KeyError:
                user = str(uid)
        except OSError:
            pass
        comm = _read(base / "comm") or ""
        try:
            raw = (base / "cmdline").read_bytes()
            command = " ".join(a.decode("utf-8", "replace") for a in raw.split(b"\0") if a)
        except OSError:
            command = ""
        result = (user, command or comm or f"[{pid}]", comm)
        self._owners[pid] = result
        return result

    def get_processes(self, device: GPUInfo) -> list[GPUProcess]:
        card = self._card(device)
        self._scan_clients()
        now = self._clock()
        merged: dict[int, dict] = {}
        for client in self._clients:
            if client.pci != card.pci:
                continue
            keys = self._parse_fdinfo(client.fdinfo)
            if not keys:
                continue  # process exited since the scan
            cid = keys.get("drm-client-id", str(client.fdinfo))
            entry = merged.setdefault(client.pid, {"busy": {}, "vram": None, "card": False, "has_eng": False,
                                                   "clients": set()})
            if cid in entry["clients"]:
                continue  # same client reachable through a dup'ed fd
            entry["clients"].add(cid)
            entry["card"] |= client.is_card_node
            engines: dict[str, int] = {}
            capacity: dict[str, int] = {}
            for k, v in keys.items():
                if k.startswith("drm-engine-capacity-"):
                    try:
                        capacity[k[len("drm-engine-capacity-"):]] = int(v)
                    except ValueError:
                        pass
                elif k.startswith("drm-engine-"):
                    try:
                        engines[k[len("drm-engine-"):]] = int(v.split()[0])
                    except (ValueError, IndexError):
                        pass
                elif k.startswith(("drm-resident-local", "drm-resident-vram")):
                    size = self._parse_size(v)
                    if size is not None:
                        entry["vram"] = (entry["vram"] or 0) + size
            if engines:
                entry["has_eng"] = True
                prev = self._proc_prev.get((client.pid, cid))
                self._proc_prev[(client.pid, cid)] = (engines, now)
                if prev is not None and now > prev[1]:
                    dt_ns = (now - prev[1]) * 1e9
                    for eng, ns in engines.items():
                        before = prev[0].get(eng)
                        if before is None:
                            continue
                        pct = 100.0 * max(0, ns - before) / (dt_ns * max(1, capacity.get(eng, 1)))
                        entry["busy"][eng] = entry["busy"].get(eng, 0.0) + pct

        procs = []
        for pid, entry in merged.items():
            user, command, comm = self._owner(pid)
            busy = entry["busy"]
            sm = round(min(100.0, max(busy.values())), 0) if busy else (0.0 if entry["has_eng"] else None)
            compute = sum(v for k, v in busy.items() if k in _COMPUTE_ENGINES)
            graphics = sum(v for k, v in busy.items() if k not in _COMPUTE_ENGINES)
            if compute > 0 and graphics > 0:
                ptype = "C+G"
            elif compute > 0 and not entry["card"]:
                ptype = "Compute"
            else:
                ptype = "Graphics"
            procs.append(GPUProcess(pid=pid, gpu_index=device.index, vram_used=entry["vram"], sm_util=sm,
                                    type=ptype, user=user, command=command, name_hint=comm))
        return procs
