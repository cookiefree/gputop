"""Backend abstraction shared by every GPU vendor implementation.

A backend only has to know how to talk to the hardware. Everything else
(history, threading, PID -> user/command resolution, rendering) lives in
vendor-neutral code, so adding AMD or Intel support means writing one new
``GPUBackend`` subclass and nothing in the UI.

Conventions used by all dataclasses below:

* ``None`` means "not supported / not available on this card or driver".
  The UI hides fields that are ``None`` instead of showing zeros.
* Memory and throughput are in **bytes** / **bytes per second**.
* Power is in **watts**, clocks in **MHz**, temperatures in **°C**,
  utilisation and fan speed in **percent (0-100)**.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Collection
from dataclasses import asdict, dataclass, field
from typing import Any


class BackendError(RuntimeError):
    """The backend cannot be used at all (library missing, no driver...)."""


class DeviceLostError(RuntimeError):
    """A device vanished or stopped responding mid-run."""

    def __init__(self, index: int, message: str = "") -> None:
        super().__init__(message or f"GPU {index} is no longer available")
        self.index = index


@dataclass(frozen=True, slots=True)
class GPUInfo:
    """Static (or rarely changing) facts about one GPU."""

    index: int
    name: str
    uuid: str = ""
    driver_version: str | None = None
    vendor: str = ""
    memory_total: int | None = None
    pcie_gen_max: int | None = None
    pcie_width_max: int | None = None
    clock_graphics_max: int | None = None
    clock_sm_max: int | None = None
    clock_mem_max: int | None = None
    power_limit_max: float | None = None
    pci_address: str = ""  # "0000:01:00.0" (sysfs form); "" if unknown
    pci_ids: str = ""  # "10de:0fe9"
    boot_vga: bool | None = None

    @property
    def short_uuid(self) -> str:
        """First block of the UUID, e.g. ``GPU-1a2b3c4d``."""
        if not self.uuid:
            return ""
        head, _, _ = self.uuid.partition("-")
        if head.upper() == "GPU":
            parts = self.uuid.split("-")
            return "-".join(parts[:2])
        return head

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class GPUSample:
    """One point-in-time measurement of a GPU."""

    index: int
    timestamp: float = field(default_factory=time.time)

    util_gpu: float | None = None
    util_mem: float | None = None
    util_estimated: bool = False  # util_gpu is an estimate (e.g. Intel RC6), not a counter

    mem_used: int | None = None
    mem_total: int | None = None

    temp_core: float | None = None
    temp_mem: float | None = None
    temp_hotspot: float | None = None
    fan: float | None = None

    power_draw: float | None = None
    power_limit: float | None = None

    clock_graphics: int | None = None
    clock_sm: int | None = None
    clock_mem: int | None = None

    pcie_tx: float | None = None
    pcie_rx: float | None = None
    pcie_gen: int | None = None
    pcie_width: int | None = None

    throttle_reasons: tuple[str, ...] | None = None

    enc_util: float | None = None
    dec_util: float | None = None

    # Busy % per engine for GPUs that report engines separately (Intel:
    # render, copy, video, ...). ``None`` when not reported.
    engine_util: dict[str, float] | None = None

    # One-line hints for the user about hidden metrics, e.g. missing
    # permissions ("GPU util hidden: needs perf access ...").
    notes: tuple[str, ...] = ()

    # Device state from PCI discovery (see gputop.pcidiscovery). ``live`` is
    # False when no backend was queried (GPU off/suspended/no driver/...):
    # then only the status fields are meaningful and history is frozen.
    live: bool = True
    status: str | None = None  # ACTIVE / SUSPENDED / OFF / NO DRIVER / UNSUPPORTED
    status_detail: str | None = None  # human explanation for non-live GPUs
    driver: str | None = None
    power_state: str | None = None  # D0 / D3hot / D3cold
    runtime_status: str | None = None  # active / suspended / unsupported

    @property
    def mem_percent(self) -> float | None:
        if self.mem_used is None or not self.mem_total:
            return None
        return 100.0 * self.mem_used / self.mem_total

    @property
    def power_percent(self) -> float | None:
        if self.power_draw is None or not self.power_limit:
            return None
        return 100.0 * self.power_draw / self.power_limit

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if data["throttle_reasons"] is not None:
            data["throttle_reasons"] = list(data["throttle_reasons"])
        data["notes"] = list(data["notes"])
        data["mem_percent"] = self.mem_percent
        return data


@dataclass(slots=True)
class GPUProcess:
    """A process using a GPU.

    Backends fill in ``pid``, ``gpu_index``, ``vram_used``, ``sm_util`` and
    ``type``. ``user`` and ``command`` are resolved afterwards by
    :mod:`gputop.procinfo` so each backend doesn't have to.
    """

    pid: int
    gpu_index: int
    vram_used: int | None = None
    sm_util: float | None = None
    type: str = "Compute"  # "Compute", "Graphics" or "C+G"
    user: str = ""
    command: str = ""
    name_hint: str = ""  # process name as reported by the driver, if any

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("name_hint", None)
        return data


# Human-readable labels for throttle reasons, shared by every backend.
THROTTLE_LABELS: dict[str, str] = {
    "idle": "IDLE",
    "app_clocks": "APP",
    "sw_power": "PWR",
    "hw_slowdown": "HW",
    "sync_boost": "SYNC",
    "sw_thermal": "THRM",
    "hw_thermal": "HWTH",
    "hw_power_brake": "BRAKE",
    "display_clocks": "DISP",
}

# Reasons that mean "the GPU is being held back" (shown as a warning) as
# opposed to benign states like idle.
THROTTLE_WARNING = frozenset(
    {"sw_power", "hw_slowdown", "sw_thermal", "hw_thermal", "hw_power_brake"}
)


class GPUBackend(ABC):
    """Interface every vendor backend implements.

    Lifecycle::

        backend = SomeBackend()
        backend.init()                 # may raise BackendError
        devices = backend.get_devices()
        sample = backend.sample(devices[0])          # may raise DeviceLostError
        procs = backend.get_processes(devices[0])    # may raise DeviceLostError
        backend.shutdown()

    ``sample`` and ``get_processes`` are called from a background thread, never
    concurrently for the same backend instance. They must never raise for a
    metric that is merely unsupported; they set that field to ``None`` instead.
    """

    name: str = "base"

    def init(self) -> None:  # noqa: B027 - optional hook
        """Open the vendor library. Raise :class:`BackendError` on failure."""

    def shutdown(self) -> None:  # noqa: B027 - optional hook
        """Release vendor resources. Must be safe to call more than once."""

    @abstractmethod
    def get_devices(self) -> list[GPUInfo]:
        """Return all GPUs this backend can see, ordered by index."""

    @abstractmethod
    def sample(self, device: GPUInfo) -> GPUSample:
        """Take one measurement of ``device``."""

    @abstractmethod
    def get_processes(self, device: GPUInfo) -> list[GPUProcess]:
        """Return the processes currently using ``device``."""

    def get_devices_for(self, addresses: Collection[str]) -> list[GPUInfo]:
        """Devices at the given PCI addresses only.

        Used for GPUs known to be awake, so a backend never has to touch a
        sleeping device. Backends that can look devices up by PCI address
        without enumerating (NVML, sysfs) override this.
        """
        want = set(addresses)
        return [d for d in self.get_devices() if d.pci_address in want]

    def refresh_info(self, device: GPUInfo) -> GPUInfo:
        """Current static info for ``device`` (may change when it wakes up)."""
        return device

    def supports_processes(self, device: GPUInfo) -> bool:
        """Whether per-process data exists for ``device`` at all.

        The UI hides the process table for devices where this is False
        (e.g. Intel without DRM fdinfo), rather than showing an empty list.
        """
        return True

    def __enter__(self) -> GPUBackend:
        self.init()
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()
