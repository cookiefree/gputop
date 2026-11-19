"""PCI discovery: every display controller on the machine, and whether it is on.

This layer runs independently of the vendor backends, so GPUs that are
powered off, runtime-suspended, unbound or bound to a driver gputop can't read
(nouveau, vfio-pci, ...) still show up.

**Read-only and non-waking.** For the status only these cheap PCI attributes
are read (none of them touch the device itself)::

    class vendor device boot_vga enable driver(symlink) power/runtime_status
    power_state  /sys/kernel/debug/vgaswitcheroo/switch (if readable)

Nothing here opens device nodes, reads hwmon/frequency/busy files, writes to
sysfs or loads modules. Backends are only ever queried for GPUs that
:func:`is_awake` says are awake (see :mod:`gputop.backends.machine`).

Status rules, first match wins:

1. vga_switcheroo reports the device ``Off``                 -> OFF
2. no driver bound: ``D3cold`` -> OFF, otherwise             -> NO DRIVER
3. driver bound but ``enable == 0`` (e.g. switched off)      -> OFF
4. ``runtime_status == suspended`` or ``D3hot``/``D3cold``     -> SUSPENDED
5. awake: a backend provides live stats                      -> ACTIVE
          otherwise (nouveau, vfio-pci, backend failed)      -> UNSUPPORTED
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path

ACTIVE = "ACTIVE"
SUSPENDED = "SUSPENDED"
OFF = "OFF"
NO_DRIVER = "NO DRIVER"
UNSUPPORTED = "UNSUPPORTED"
STATUSES = (ACTIVE, SUSPENDED, OFF, NO_DRIVER, UNSUPPORTED)

VENDOR_NAMES = {0x10DE: "NVIDIA", 0x8086: "Intel", 0x1002: "AMD", 0x1022: "AMD", 0x15AD: "VMware",
                0x1AF4: "virtio", 0x1234: "QEMU", 0x102B: "Matrox", 0x1A03: "ASPEED"}
PCI_IDS_PATHS = ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids", "/usr/share/pci.ids")
SWITCHEROO = "/sys/kernel/debug/vgaswitcheroo/switch"


@dataclass(frozen=True)
class PciGpu:
    """Static facts about one PCI display controller."""

    address: str  # "0000:01:00.0"
    vendor_id: int
    device_id: int
    class_id: int
    vendor: str  # "NVIDIA"
    name: str  # "NVIDIA GK107M [GeForce GT 750M Mac Edition]"
    boot_vga: bool | None

    @property
    def ids(self) -> str:
        return f"{self.vendor_id:04x}:{self.device_id:04x}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ids"] = self.ids
        return d


@dataclass(frozen=True)
class PciPower:
    """Cheap, non-waking power/driver attributes (re-read every refresh)."""

    driver: str | None
    enable: int | None
    runtime_status: str | None  # active / suspended / unsupported / ...
    power_state: str | None  # D0 / D3hot / D3cold / unknown
    switcheroo: str | None = None  # "On" / "Off" / "DynOff" / "DynPwr" if readable

    @property
    def awake(self) -> bool:
        """Safe to query: bound, enabled and not in a low-power state."""
        return classify(self, live=False) == UNSUPPORTED


def classify(p: PciPower, live: bool) -> str:
    """Status for a device; ``live`` = a backend can read it."""
    if p.switcheroo is not None and p.switcheroo.lower() in ("off", "dynoff"):
        return OFF
    if p.driver is None:
        return OFF if p.power_state == "D3cold" else NO_DRIVER
    if p.enable == 0:
        return OFF
    if p.runtime_status == "suspended" or p.power_state in ("D3hot", "D3cold"):
        return SUSPENDED
    return ACTIVE if live else UNSUPPORTED


def describe(status: str, p: PciPower, reason: str | None = None) -> str:
    """One-line explanation shown in the GPU box for non-live GPUs."""
    if status == OFF:
        return "GPU is off - no data"
    if status == SUSPENDED:
        state = p.power_state if p.power_state and p.power_state != "D0" else p.runtime_status
        return f"suspended ({state}) - not queried to avoid waking it"
    if status == NO_DRIVER:
        return "no driver loaded"
    if status == UNSUPPORTED:
        msg = f"driver {p.driver} - no stats available"
        return f"{msg} ({reason})" if reason else msg
    return ""


# -- reading sysfs ---------------------------------------------------------------


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, ValueError):
        return None


def _read_int(path: Path) -> int | None:
    text = _read(path)
    try:
        return int(text, 0) if text else None
    except ValueError:
        return None


class PciDiscovery:
    def __init__(self, root: str | os.PathLike[str] = "/") -> None:
        self.root = Path(root)
        self._names: dict[tuple[int, int], str] = {}

    def _p(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def device_dir(self, address: str) -> Path:
        return self._p("/sys/bus/pci/devices") / address

    def discover(self) -> list[PciGpu]:
        """All PCI display controllers (class 0x03xxxx), sorted by address."""
        base = self._p("/sys/bus/pci/devices")
        try:
            addresses = sorted(os.listdir(base))
        except OSError:
            return []
        gpus = []
        for addr in addresses:
            d = base / addr
            cls = _read_int(d / "class")
            if cls is None or (cls >> 16) != 0x03:
                continue
            vendor_id = _read_int(d / "vendor") or 0
            device_id = _read_int(d / "device") or 0
            boot = _read_int(d / "boot_vga")
            gpus.append(PciGpu(
                address=addr,
                vendor_id=vendor_id,
                device_id=device_id,
                class_id=cls,
                vendor=VENDOR_NAMES.get(vendor_id, f"{vendor_id:04x}"),
                name=self.name(vendor_id, device_id),
                boot_vga=None if boot is None else bool(boot),
            ))
        return gpus

    def power(self, address: str) -> PciPower | None:
        """Re-read the cheap status attributes; None if the device vanished."""
        d = self.device_dir(address)
        if not d.exists():
            return None
        try:
            driver = os.path.basename(os.readlink(d / "driver"))
        except OSError:
            driver = None
        return PciPower(
            driver=driver,
            enable=_read_int(d / "enable"),
            runtime_status=_read(d / "power" / "runtime_status"),
            power_state=_read(d / "power_state"),
            switcheroo=self._switcheroo().get(address),
        )

    def _switcheroo(self) -> dict[str, str]:
        """vga_switcheroo states by PCI address (debugfs; usually root-only)."""
        text = _read(self._p(SWITCHEROO))
        out: dict[str, str] = {}
        for line in (text or "").splitlines():
            # "1:DIS: :Off:0000:01:00.0"
            parts = line.split(":")
            if len(parts) >= 7:
                out[":".join(parts[-3:])] = parts[3]
        return out

    # -- names ------------------------------------------------------------------

    def name(self, vendor_id: int, device_id: int) -> str:
        key = (vendor_id, device_id)
        if key not in self._names:
            vendor = VENDOR_NAMES.get(vendor_id, f"{vendor_id:04x}")
            dev = self._lookup_pci_ids(vendor_id, device_id)
            self._names[key] = f"{vendor} {dev}" if dev else f"{vendor} {device_id:04x}"
        return self._names[key]

    def _lookup_pci_ids(self, vendor_id: int, device_id: int) -> str | None:
        vend = f"{vendor_id:04x}  "
        want = f"\t{device_id:04x}  "
        for path in PCI_IDS_PATHS:
            try:
                with open(self._p(path), encoding="utf-8", errors="replace") as fh:
                    in_vendor = False
                    for line in fh:
                        if line.startswith(vend):
                            in_vendor = True
                        elif in_vendor:
                            if line.startswith(want):
                                return line[len(want):].strip()
                            if line and not line.startswith(("\t", "#")):
                                return None
            except OSError:
                continue
        return None


def normalize_address(addr: str) -> str:
    """'00000000:01:00.0' (NVML) -> '0000:01:00.0' (sysfs), lower case."""
    a = addr.strip().lower()
    parts = a.split(":")
    if len(parts) == 3:
        dom, bus, devfn = parts
        return f"{int(dom, 16) & 0xFFFF:04x}:{bus}:{devfn}"
    if len(parts) == 2:
        return f"0000:{a}"
    return a
