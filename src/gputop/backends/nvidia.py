"""NVIDIA backend using NVML through ``nvidia-ml-py`` (``import pynvml``).

Every NVML call goes through :meth:`NvidiaBackend._q`, which turns "not
supported", "no permission" and similar errors into ``None`` so the field is
simply hidden, and turns "GPU is lost" into :class:`DeviceLostError`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

from ..pcidiscovery import normalize_address
from .base import BackendError, DeviceLostError, GPUBackend, GPUInfo, GPUProcess, GPUSample

log = logging.getLogger(__name__)
T = TypeVar("T")

# NVML clock-event / throttle-reason bit masks (stable across driver versions).
_THROTTLE_BITS: tuple[tuple[int, str], ...] = (
    (0x0000000000000001, "idle"),
    (0x0000000000000002, "app_clocks"),
    (0x0000000000000004, "sw_power"),
    (0x0000000000000008, "hw_slowdown"),
    (0x0000000000000010, "sync_boost"),
    (0x0000000000000020, "sw_thermal"),
    (0x0000000000000040, "hw_thermal"),
    (0x0000000000000080, "hw_power_brake"),
    (0x0000000000000100, "display_clocks"),
)

# NVML field id for memory (HBM/GDDR) temperature, if the driver supports it.
_FI_DEV_MEMORY_TEMP = 82


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


class NvidiaBackend(GPUBackend):
    name = "nvidia"

    def __init__(self) -> None:
        self._nvml: Any = None
        self._handles: dict[int, Any] = {}
        self._addr_index: dict[str, int] = {}
        self._initialised = False
        self._driver: str | None = None
        self._proc_util_ts: dict[int, int] = {}
        self._lost_codes: set[int] = set()
        self._soft_codes: set[int] = set()

    # -- lifecycle ------------------------------------------------------------

    def init(self) -> None:
        try:
            import pynvml
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise BackendError(
                "the 'nvidia-ml-py' package is not installed (pip install nvidia-ml-py)"
            ) from exc
        self._nvml = pynvml
        try:
            # NO_ATTACH: don't attach to (and possibly wake) every GPU at init;
            # handles are attached one by one, only for GPUs known to be awake.
            init_flags = getattr(pynvml, "nvmlInitWithFlags", None)
            no_attach = getattr(pynvml, "NVML_INIT_FLAG_NO_ATTACH", None)
            if init_flags is not None and no_attach is not None:
                init_flags(no_attach)
            else:
                pynvml.nvmlInit()
        except pynvml.NVMLError as exc:
            raise BackendError(self._explain_init_error(exc)) from exc
        self._initialised = True

        def code(name: str) -> int | None:
            return getattr(pynvml, name, None)

        self._lost_codes = {c for c in (code("NVML_ERROR_GPU_IS_LOST"),) if c is not None}
        self._soft_codes = {
            c
            for c in (
                code("NVML_ERROR_NOT_SUPPORTED"),
                code("NVML_ERROR_NO_PERMISSION"),
                code("NVML_ERROR_NOT_FOUND"),
                code("NVML_ERROR_INVALID_ARGUMENT"),
                code("NVML_ERROR_FUNCTION_NOT_FOUND"),
                code("NVML_ERROR_UNKNOWN"),
                code("NVML_ERROR_TIMEOUT"),
                code("NVML_ERROR_INSUFFICIENT_SIZE"),
                code("NVML_ERROR_ARGUMENT_VERSION_MISMATCH"),
            )
            if c is not None
        }
        self._driver = self._q(lambda: _decode(pynvml.nvmlSystemGetDriverVersion()))

    def _explain_init_error(self, exc: Exception) -> str:
        nv = self._nvml
        value = getattr(exc, "value", None)
        if value == getattr(nv, "NVML_ERROR_LIBRARY_NOT_FOUND", object()):
            return (
                "NVML library (libnvidia-ml.so) not found. Is the proprietary NVIDIA "
                "driver installed? (Run with --backend fake to try gputop without a GPU.)"
            )
        if value == getattr(nv, "NVML_ERROR_DRIVER_NOT_LOADED", object()):
            return (
                "the NVIDIA kernel driver is not loaded (nouveau or no driver in use?). "
                "Run with --backend fake to try gputop without a GPU."
            )
        if value == getattr(nv, "NVML_ERROR_NO_PERMISSION", object()):
            return "no permission to access the NVIDIA driver (check /dev/nvidia* permissions)"
        return f"could not initialise NVML: {exc}"

    def shutdown(self) -> None:
        if self._initialised and self._nvml is not None:
            try:
                self._nvml.nvmlShutdown()
            except Exception:  # noqa: BLE001 - shutting down anyway
                pass
        self._initialised = False
        self._handles.clear()
        self._addr_index.clear()

    # -- error handling -------------------------------------------------------

    def _q(self, fn: Callable[[], T], index: int | None = None) -> T | None:
        """Call ``fn``; unsupported -> None, lost GPU -> DeviceLostError."""
        try:
            return fn()
        except self._nvml.NVMLError as exc:
            value = getattr(exc, "value", None)
            if value in self._lost_codes and index is not None:
                raise DeviceLostError(index, f"GPU {index} is lost: {exc}") from exc
            if value not in self._soft_codes:
                log.debug("NVML call failed: %s", exc)
            return None
        except (AttributeError, TypeError, ValueError) as exc:
            # Older pynvml without this function, or odd return types.
            log.debug("NVML call unavailable: %s", exc)
            return None

    def _handle(self, device: GPUInfo) -> Any:
        h = self._handles.get(device.index)
        if h is None:
            nv = self._nvml
            h = self._q(lambda: nv.nvmlDeviceGetHandleByIndex(device.index), device.index)
            if h is None:
                raise DeviceLostError(device.index)
            self._handles[device.index] = h
        return h

    # -- discovery ------------------------------------------------------------

    def get_devices(self) -> list[GPUInfo]:
        if not self._initialised:
            self.init()
        nv = self._nvml
        try:
            count = nv.nvmlDeviceGetCount()
        except nv.NVMLError as exc:
            raise BackendError(f"could not enumerate GPUs: {exc}") from exc
        devices = []
        for i in range(count):
            h = self._q(lambda i=i: nv.nvmlDeviceGetHandleByIndex(i))
            if h is None:
                continue
            self._handles[i] = h
            devices.append(self._info(i, h))
        return devices

    def get_devices_for(self, addresses) -> list[GPUInfo]:
        """Look GPUs up by PCI bus id, so other (possibly sleeping) GPUs are
        never enumerated or attached."""
        if not self._initialised:
            self.init()
        nv = self._nvml
        lookup = getattr(nv, "nvmlDeviceGetHandleByPciBusId_v2", None) or getattr(
            nv, "nvmlDeviceGetHandleByPciBusId", None)
        if lookup is None:
            return super().get_devices_for(addresses)
        devices = []
        for addr in addresses:
            h = self._q(lambda addr=addr: lookup(addr))
            if h is None:
                continue
            index = self._addr_index.setdefault(addr, 1000 + len(self._addr_index))
            self._handles[index] = h
            devices.append(self._info(index, h))
        return devices

    def _info(self, i: int, h) -> GPUInfo:
        nv = self._nvml
        mem = self._q(lambda: nv.nvmlDeviceGetMemoryInfo(h))
        limit_max = self._q(lambda: nv.nvmlDeviceGetPowerManagementLimitConstraints(h))
        pci_info = self._q(lambda: nv.nvmlDeviceGetPciInfo(h))
        bus = ""
        if pci_info is not None:
            raw = getattr(pci_info, "busId", None) or getattr(pci_info, "busIdLegacy", b"")
            bus = normalize_address(_decode(raw)) if raw else ""
        return GPUInfo(
            index=i,
            name=_decode(self._q(lambda: nv.nvmlDeviceGetName(h)) or f"NVIDIA GPU {i}"),
            uuid=_decode(self._q(lambda: nv.nvmlDeviceGetUUID(h)) or ""),
            driver_version=self._driver,
            vendor="NVIDIA",
            memory_total=int(mem.total) if mem is not None else None,
            pcie_gen_max=self._q(lambda: nv.nvmlDeviceGetMaxPcieLinkGeneration(h)),
            pcie_width_max=self._q(lambda: nv.nvmlDeviceGetMaxPcieLinkWidth(h)),
            clock_graphics_max=self._q(lambda: nv.nvmlDeviceGetMaxClockInfo(h, nv.NVML_CLOCK_GRAPHICS)),
            clock_sm_max=self._q(lambda: nv.nvmlDeviceGetMaxClockInfo(h, nv.NVML_CLOCK_SM)),
            clock_mem_max=self._q(lambda: nv.nvmlDeviceGetMaxClockInfo(h, nv.NVML_CLOCK_MEM)),
            power_limit_max=(limit_max[1] / 1000.0) if limit_max else None,
            pci_address=bus,
        )

    # -- sampling -------------------------------------------------------------

    def sample(self, device: GPUInfo) -> GPUSample:
        nv = self._nvml
        i = device.index
        h = self._handle(device)

        def q(fn: Callable[[], T]) -> T | None:
            return self._q(fn, i)

        s = GPUSample(index=i)

        util = q(lambda: nv.nvmlDeviceGetUtilizationRates(h))
        if util is not None:
            s.util_gpu = float(util.gpu)
            s.util_mem = float(util.memory)

        mem = q(lambda: nv.nvmlDeviceGetMemoryInfo(h))
        if mem is not None:
            s.mem_used = int(mem.used)
            s.mem_total = int(mem.total)

        s.temp_core = q(lambda: float(nv.nvmlDeviceGetTemperature(h, nv.NVML_TEMPERATURE_GPU)))
        s.temp_mem = self._field_value(h, _FI_DEV_MEMORY_TEMP, i)
        # NVML exposes no public hotspot/junction sensor; temp_hotspot stays None.
        s.fan = q(lambda: float(nv.nvmlDeviceGetFanSpeed(h)))

        power = q(lambda: nv.nvmlDeviceGetPowerUsage(h))
        s.power_draw = power / 1000.0 if power is not None else None
        limit = q(lambda: nv.nvmlDeviceGetEnforcedPowerLimit(h))
        if limit is None:
            limit = q(lambda: nv.nvmlDeviceGetPowerManagementLimit(h))
        s.power_limit = limit / 1000.0 if limit else None

        s.clock_graphics = q(lambda: nv.nvmlDeviceGetClockInfo(h, nv.NVML_CLOCK_GRAPHICS))
        s.clock_sm = q(lambda: nv.nvmlDeviceGetClockInfo(h, nv.NVML_CLOCK_SM))
        s.clock_mem = q(lambda: nv.nvmlDeviceGetClockInfo(h, nv.NVML_CLOCK_MEM))

        # Throughput is reported in KB/s; each call samples for ~20 ms.
        tx = q(lambda: nv.nvmlDeviceGetPcieThroughput(h, nv.NVML_PCIE_UTIL_TX_BYTES))
        rx = q(lambda: nv.nvmlDeviceGetPcieThroughput(h, nv.NVML_PCIE_UTIL_RX_BYTES))
        s.pcie_tx = tx * 1024.0 if tx is not None else None
        s.pcie_rx = rx * 1024.0 if rx is not None else None
        s.pcie_gen = q(lambda: nv.nvmlDeviceGetCurrPcieLinkGeneration(h))
        s.pcie_width = q(lambda: nv.nvmlDeviceGetCurrPcieLinkWidth(h))

        s.throttle_reasons = self._throttle(h, i)

        enc = q(lambda: nv.nvmlDeviceGetEncoderUtilization(h))
        dec = q(lambda: nv.nvmlDeviceGetDecoderUtilization(h))
        s.enc_util = float(enc[0]) if enc else None
        s.dec_util = float(dec[0]) if dec else None
        return s

    def _field_value(self, h: Any, field_id: int, index: int) -> float | None:
        nv = self._nvml
        values = self._q(lambda: nv.nvmlDeviceGetFieldValues(h, [field_id]), index)
        if not values:
            return None
        fv = values[0]
        if getattr(fv, "nvmlReturn", 1) != 0:
            return None
        v = getattr(fv, "value", None)
        for attr in ("uiVal", "ullVal", "ulVal", "sllVal", "dVal"):
            if v is not None and hasattr(v, attr):
                val = getattr(v, attr)
                if val:
                    return float(val)
        return None

    def _throttle(self, h: Any, index: int) -> tuple[str, ...] | None:
        nv = self._nvml
        fn = getattr(nv, "nvmlDeviceGetCurrentClocksEventReasons", None) or getattr(
            nv, "nvmlDeviceGetCurrentClocksThrottleReasons", None
        )
        if fn is None:
            return None
        mask = self._q(lambda: fn(h), index)
        if mask is None:
            return None
        return tuple(name for bit, name in _THROTTLE_BITS if mask & bit)

    # -- processes ------------------------------------------------------------

    def get_processes(self, device: GPUInfo) -> list[GPUProcess]:
        nv = self._nvml
        i = device.index
        h = self._handle(device)

        def running(kind: str) -> list[Any]:
            for suffix in ("_v3", "_v2", ""):
                fn = getattr(nv, f"nvmlDeviceGet{kind}RunningProcesses{suffix}", None)
                if fn is not None:
                    res = self._q(lambda fn=fn: fn(h), i)
                    if res is not None:
                        return list(res)
            return []

        by_pid: dict[int, GPUProcess] = {}
        not_available = getattr(nv, "NVML_VALUE_NOT_AVAILABLE", None)
        for kind, label in (("Compute", "Compute"), ("Graphics", "Graphics")):
            for p in running(kind):
                pid = int(p.pid)
                mem = getattr(p, "usedGpuMemory", None)
                if mem is None or mem == not_available or mem < 0 or mem >= 2**63:
                    mem = None
                if pid in by_pid:
                    existing = by_pid[pid]
                    if existing.type != label:
                        existing.type = "C+G"
                    if mem is not None:
                        existing.vram_used = max(existing.vram_used or 0, mem)
                else:
                    by_pid[pid] = GPUProcess(pid=pid, gpu_index=i, vram_used=mem, type=label)

        if by_pid:
            util = self._process_utilization(h, i)
            if util is not None:
                for pid, proc in by_pid.items():
                    proc.sm_util = util.get(pid, 0.0)
        return list(by_pid.values())

    def _process_utilization(self, h: Any, index: int) -> dict[int, float] | None:
        """Per-process SM% from NVML's sample buffer since our last query.

        Returns ``None`` if per-process utilisation is unsupported, and an
        empty dict if it is supported but no process was active (NVML reports
        that as NOT_FOUND), so callers can show 0% instead of "-".
        """
        nv = self._nvml
        fn = getattr(nv, "nvmlDeviceGetProcessUtilization", None)
        if fn is None:
            return None
        last = self._proc_util_ts.get(index, 0)
        try:
            samples = fn(h, last)
        except nv.NVMLError as exc:
            value = getattr(exc, "value", None)
            if value in self._lost_codes:
                raise DeviceLostError(index, f"GPU {index} is lost: {exc}") from exc
            if value == getattr(nv, "NVML_ERROR_NOT_FOUND", None):
                return {}
            return None
        except (AttributeError, TypeError, ValueError):
            return None
        out: dict[int, float] = {}
        newest = last
        for smp in samples or ():
            pid = int(smp.pid)
            out[pid] = max(out.get(pid, 0.0), float(smp.smUtil))
            newest = max(newest, int(smp.timeStamp))
        self._proc_util_ts[index] = newest
        return out
