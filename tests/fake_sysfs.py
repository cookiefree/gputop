"""Build a fake sysfs/procfs tree for the Intel backend tests.

``IntelBackend(root=tree.root, counter_opener=tree.open_counter, clock=tree.clock)``
reads only from the tree, so tests need no Intel GPU, no permissions and never
touch the real /sys or /proc.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class FakeCounter:
    def __init__(self, tree: FakeSysfs, config: int) -> None:
        self.tree, self.config, self.closed = tree, config, False

    def read(self) -> tuple[int, int]:
        return self.tree.pmu_values.get(self.config, 0), int(self.tree.clock.t * 1e9)

    def close(self) -> None:
        self.closed = True


class FakeSysfs:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.clock = FakeClock()
        self.pmu_values: dict[int, int] = {}  # event config -> busy ns
        self.pmu_error: int | None = None  # errno raised by open_counter
        self.counters: list[FakeCounter] = []
        self.write("proc/sys/kernel/osrelease", "6.9.0-test")
        self.write("proc/sys/kernel/perf_event_paranoid", "2")
        (self.root / "sys/class/drm").mkdir(parents=True, exist_ok=True)

    # -- helpers --------------------------------------------------------------

    def write(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n")
        return p

    def open_counter(self, pmu_type: int, config: int, cpu: int) -> FakeCounter:
        if self.pmu_error is not None:
            raise OSError(self.pmu_error, os.strerror(self.pmu_error))
        c = FakeCounter(self, config)
        self.counters.append(c)
        return c

    # -- builders -------------------------------------------------------------

    def add_card(
        self,
        n: int,
        pci: str = "0000:00:02.0",
        vendor: str = "0x8086",
        device_id: str = "0x9a49",
        driver: str = "i915",
        freq: dict[str, int] | None = None,
        freq_style: str = "card",  # "card", "gt" (gt/gt0/rps_*) or "xe"
        throttle: dict[str, int] | None = None,
        connectors: tuple[str, ...] = ("eDP-1",),
    ) -> Path:
        dev = self.root / "sys/devices/pci0000:00" / pci
        dev.mkdir(parents=True, exist_ok=True)
        self.write(f"sys/devices/pci0000:00/{pci}/vendor", vendor)
        self.write(f"sys/devices/pci0000:00/{pci}/device", device_id)
        self.write(f"sys/devices/pci0000:00/{pci}/class", "0x030000")
        drv = self.root / "sys/bus/pci/drivers" / driver
        drv.mkdir(parents=True, exist_ok=True)
        if not (dev / "driver").exists():
            os.symlink(drv, dev / "driver")
        card = self.root / f"sys/class/drm/card{n}"
        card.mkdir(parents=True, exist_ok=True)
        os.symlink(dev, card / "device")
        for c in connectors:  # connector entries must be ignored
            (self.root / f"sys/class/drm/card{n}-{c}").mkdir(exist_ok=True)
        freq = {"act": 300, "cur": 350, "max": 1300} if freq is None else freq
        for key, value in freq.items():
            if freq_style == "card":
                self.write(f"sys/class/drm/card{n}/gt_{key}_freq_mhz", str(value))
            elif freq_style == "gt":
                self.write(f"sys/class/drm/card{n}/gt/gt0/rps_{key}_freq_mhz", str(value))
            else:
                self.write(f"sys/devices/pci0000:00/{pci}/tile0/gt0/freq0/{key}_freq", str(value))
        if throttle is not None:
            self.write(f"sys/class/drm/card{n}/gt/gt0/throttle_reason_status", str(int(any(throttle.values()))))
            for key, value in throttle.items():
                self.write(f"sys/class/drm/card{n}/gt/gt0/throttle_reason_{key}", str(value))
        return dev

    def add_card_hwmon(self, pci: str, temp_mc: int | None = None, energy_uj: int | None = None,
                       power_max_uw: int | None = None) -> Path:
        hw = self.root / "sys/devices/pci0000:00" / pci / "hwmon/hwmon7"
        hw.mkdir(parents=True, exist_ok=True)
        (hw / "name").write_text("i915\n")
        if temp_mc is not None:
            (hw / "temp1_input").write_text(f"{temp_mc}\n")
        if energy_uj is not None:
            (hw / "energy1_input").write_text(f"{energy_uj}\n")
        if power_max_uw is not None:
            (hw / "power1_max").write_text(f"{power_max_uw}\n")
        return hw

    def set_link(self, pci: str, cur: str = "16.0 GT/s PCIe", width: int = 16, max_speed: str = "16.0 GT/s PCIe",
                 max_width: int = 16) -> None:
        base = f"sys/devices/pci0000:00/{pci}"
        self.write(f"{base}/current_link_speed", cur)
        self.write(f"{base}/current_link_width", str(width))
        self.write(f"{base}/max_link_speed", max_speed)
        self.write(f"{base}/max_link_width", str(max_width))

    def add_rapl(self, zone: str, name: str, energy_uj: int | None, max_range: int = 262143328850,
                 limit_uw: int | None = None, readable: bool = True) -> Path:
        z = self.root / "sys/class/powercap" / zone
        z.mkdir(parents=True, exist_ok=True)
        (z / "name").write_text(name + "\n")
        (z / "max_energy_range_uj").write_text(f"{max_range}\n")
        if limit_uw is not None:
            (z / "constraint_0_power_limit_uw").write_text(f"{limit_uw}\n")
        if energy_uj is not None:
            e = z / "energy_uj"
            e.write_text(f"{energy_uj}\n")
            if not readable:
                e.chmod(0)
        return z

    def set_energy(self, zone_dir: Path, energy_uj: int, file: str = "energy_uj") -> None:
        (zone_dir / file).write_text(f"{energy_uj}\n")

    def add_coretemp(self, package_mc: int = 61000, core_mc: int = 55000) -> None:
        hw = self.root / "sys/class/hwmon/hwmon4"
        hw.mkdir(parents=True, exist_ok=True)
        (hw / "name").write_text("coretemp\n")
        (hw / "temp1_label").write_text("Package id 0\n")
        (hw / "temp1_input").write_text(f"{package_mc}\n")
        (hw / "temp2_label").write_text("Core 0\n")
        (hw / "temp2_input").write_text(f"{core_mc}\n")
        other = self.root / "sys/class/hwmon/hwmon1"
        other.mkdir(parents=True, exist_ok=True)
        (other / "name").write_text("BAT0\n")

    def add_pmu(self, name: str = "i915", engines: dict[str, int] | None = None, pmu_type: int = 11) -> None:
        engines = {"rcs0": 0x0, "bcs0": 0x100000, "vcs0": 0x200000} if engines is None else engines
        base = f"sys/bus/event_source/devices/{name}"
        self.write(f"{base}/type", str(pmu_type))
        self.write(f"{base}/cpumask", "0")
        for engine, config in engines.items():
            self.write(f"{base}/events/{engine}-busy", f"config={config:#x}")
            self.write(f"{base}/events/{engine}-busy.unit", "ns")
            self.write(f"{base}/events/{engine}-wait", f"config={config + 1:#x}")
        self.write(f"{base}/events/actual-frequency", "config=0x100000")

    def add_pci_ids(self, entries: dict[int, str]) -> None:
        lines = ["# fake pci.ids", "10de  NVIDIA Corporation", "\t0fe9  GK107M", "8086  Intel Corporation"]
        lines += [f"\t{dev:04x}  {name}" for dev, name in entries.items()]
        lines += ["8087  Intel Corporation (other)"]
        self.write("usr/share/hwdata/pci.ids", "\n".join(lines))

    def add_proc(self, pid: int, comm: str, cmdline: list[str], fds: dict[int, tuple[str, str | None]]) -> Path:
        """``fds``: {fd: (link target, fdinfo text or None)}."""
        base = self.root / "proc" / str(pid)
        (base / "fd").mkdir(parents=True, exist_ok=True)
        (base / "fdinfo").mkdir(parents=True, exist_ok=True)
        (base / "comm").write_text(comm + "\n")
        (base / "cmdline").write_bytes(b"\0".join(a.encode() for a in cmdline) + b"\0")
        for fd, (target, info) in fds.items():
            os.symlink(target, base / "fd" / str(fd))
            (base / "fdinfo" / str(fd)).write_text(info if info is not None else "pos:\t0\nflags:\t02\n")
        return base


def drm_fdinfo(pci: str = "0000:00:02.0", client_id: int = 1, engines: dict[str, int] | None = None,
               capacity: dict[str, int] | None = None, local_kib: int | None = None,
               driver: str = "i915") -> str:
    lines = ["pos:\t0", "flags:\t02100002", f"drm-driver:\t{driver}", f"drm-client-id:\t{client_id}",
             f"drm-pdev:\t{pci}", "drm-total-system0:\t1024 KiB", "drm-resident-system0:\t1024 KiB"]
    if local_kib is not None:
        lines.append(f"drm-resident-local0:\t{local_kib} KiB")
    for name, ns in (engines or {}).items():
        lines.append(f"drm-engine-{name}:\t{ns} ns")
    for name, cap in (capacity or {}).items():
        lines.append(f"drm-engine-capacity-{name}:\t{cap}")
    return "\n".join(lines) + "\n"


__all__ = ["FakeSysfs", "drm_fdinfo", "errno"]
