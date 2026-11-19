"""NVIDIA backend tests against a mocked ``pynvml`` (no driver needed).

These check our handling of NVML's quirks: unsupported metrics, missing
functions on older bindings, merged compute+graphics processes, per-process
utilisation semantics and a GPU falling off the bus.
"""

import sys
import types
from types import SimpleNamespace as NS

import pytest

from gputop.backends.base import BackendError, DeviceLostError
from gputop.sampler import Sampler

NOT_SUPPORTED, NO_PERMISSION, NOT_FOUND, GPU_IS_LOST = 3, 4, 6, 15
LIBRARY_NOT_FOUND, DRIVER_NOT_LOADED = 12, 9


class NVMLError(Exception):
    def __init__(self, value):
        super().__init__(f"NVML error {value}")
        self.value = value


def make_pynvml(gpus, fail_init=None):
    """Build a fake pynvml module. ``gpus`` is a list of dicts of overrides."""
    m = types.ModuleType("pynvml")
    m.NVMLError = NVMLError
    m.NVML_ERROR_NOT_SUPPORTED = NOT_SUPPORTED
    m.NVML_ERROR_NO_PERMISSION = NO_PERMISSION
    m.NVML_ERROR_NOT_FOUND = NOT_FOUND
    m.NVML_ERROR_GPU_IS_LOST = GPU_IS_LOST
    m.NVML_ERROR_LIBRARY_NOT_FOUND = LIBRARY_NOT_FOUND
    m.NVML_ERROR_DRIVER_NOT_LOADED = DRIVER_NOT_LOADED
    m.NVML_TEMPERATURE_GPU = 0
    m.NVML_CLOCK_GRAPHICS, m.NVML_CLOCK_SM, m.NVML_CLOCK_MEM = 0, 1, 2
    m.NVML_PCIE_UTIL_TX_BYTES, m.NVML_PCIE_UTIL_RX_BYTES = 0, 1
    m.NVML_VALUE_NOT_AVAILABLE = 2**64 - 1
    m.state = {"lost": set(), "shutdown": 0}

    def get(h, key, default=None):
        if h in m.state["lost"]:
            raise NVMLError(GPU_IS_LOST)
        v = gpus[h].get(key, default)
        if isinstance(v, Exception):
            raise v
        if v is None:
            raise NVMLError(NOT_SUPPORTED)
        return v

    def init():
        if fail_init is not None:
            raise NVMLError(fail_init)

    m.nvmlInit = init
    m.nvmlShutdown = lambda: m.state.__setitem__("shutdown", m.state["shutdown"] + 1)
    m.nvmlSystemGetDriverVersion = lambda: b"555.42.02"
    m.nvmlDeviceGetCount = lambda: len(gpus)
    m.nvmlDeviceGetHandleByIndex = lambda i: i
    m.nvmlDeviceGetName = lambda h: get(h, "name", "NVIDIA GeForce RTX 4070")
    m.nvmlDeviceGetUUID = lambda h: get(h, "uuid", f"GPU-{h:08x}-aaaa-bbbb")
    m.nvmlDeviceGetMemoryInfo = lambda h: get(h, "mem", NS(used=3 << 30, total=12 << 30))
    m.nvmlDeviceGetPowerManagementLimitConstraints = lambda h: get(h, "limits", (100_000, 220_000))
    m.nvmlDeviceGetMaxPcieLinkGeneration = lambda h: get(h, "pcie_gen_max", 4)
    m.nvmlDeviceGetMaxPcieLinkWidth = lambda h: get(h, "pcie_width_max", 16)
    m.nvmlDeviceGetMaxClockInfo = lambda h, c: get(h, f"max_clock{c}", [2475, 2475, 10501][c])
    m.nvmlDeviceGetUtilizationRates = lambda h: get(h, "util", NS(gpu=87, memory=40))
    m.nvmlDeviceGetTemperature = lambda h, s: get(h, "temp", 71)
    m.nvmlDeviceGetFieldValues = lambda h, ids: get(
        h, "fields", [NS(nvmlReturn=0, value=NS(uiVal=80, ullVal=80))]
    )
    m.nvmlDeviceGetFanSpeed = lambda h: get(h, "fan", 58)
    m.nvmlDeviceGetPowerUsage = lambda h: get(h, "power", 178_000)
    m.nvmlDeviceGetEnforcedPowerLimit = lambda h: get(h, "limit", 200_000)
    m.nvmlDeviceGetPowerManagementLimit = lambda h: get(h, "limit_fallback", None)
    m.nvmlDeviceGetClockInfo = lambda h, c: get(h, f"clock{c}", [2400, 2400, 10501][c])
    m.nvmlDeviceGetPcieThroughput = lambda h, c: get(h, f"pcie{c}", [1000, 4000][c])
    m.nvmlDeviceGetCurrPcieLinkGeneration = lambda h: get(h, "pcie_gen", 4)
    m.nvmlDeviceGetCurrPcieLinkWidth = lambda h: get(h, "pcie_width", 16)
    m.nvmlDeviceGetCurrentClocksEventReasons = lambda h: get(h, "throttle", 0x4 | 0x20)
    m.nvmlDeviceGetEncoderUtilization = lambda h: get(h, "enc", [12, 1000])
    m.nvmlDeviceGetDecoderUtilization = lambda h: get(h, "dec", [3, 1000])
    m.nvmlDeviceGetComputeRunningProcesses_v3 = lambda h: get(h, "compute", [])
    m.nvmlDeviceGetGraphicsRunningProcesses_v3 = lambda h: get(h, "graphics", [])
    m.nvmlDeviceGetProcessUtilization = lambda h, ts: get(h, "proc_util", NVMLError(NOT_FOUND))
    return m


@pytest.fixture
def nvml(monkeypatch):
    def install(gpus, **kwargs):
        mod = make_pynvml(gpus, **kwargs)
        monkeypatch.setitem(sys.modules, "pynvml", mod)
        from gputop.backends.nvidia import NvidiaBackend

        return NvidiaBackend(), mod

    return install


def test_full_featured_gpu(nvml):
    backend, _ = nvml([{}])
    backend.init()
    (dev,) = backend.get_devices()
    assert dev.name == "NVIDIA GeForce RTX 4070"
    assert dev.driver_version == "555.42.02"
    assert dev.memory_total == 12 << 30
    assert dev.power_limit_max == 220.0
    assert dev.pcie_gen_max == 4 and dev.clock_graphics_max == 2475

    s = backend.sample(dev)
    assert (s.util_gpu, s.util_mem) == (87.0, 40.0)
    assert s.mem_used == 3 << 30
    assert s.temp_core == 71 and s.temp_mem == 80.0 and s.temp_hotspot is None
    assert s.fan == 58
    assert s.power_draw == 178.0 and s.power_limit == 200.0
    assert s.clock_graphics == 2400 and s.clock_mem == 10501
    assert s.pcie_tx == 1000 * 1024 and s.pcie_rx == 4000 * 1024
    assert s.throttle_reasons == ("sw_power", "sw_thermal")
    assert s.enc_util == 12 and s.dec_util == 3


def test_unsupported_metrics_become_none(nvml):
    # A laptop GPU: no power limit, no fan, no PCIe counters, no codec, no
    # memory temperature, and NVML returns permission errors for some calls.
    backend, _ = nvml([{
        "limit": None, "limit_fallback": None, "limits": None, "fan": None,
        "pcie0": None, "pcie1": None, "enc": None, "dec": None,
        "fields": [NS(nvmlReturn=NOT_SUPPORTED, value=NS(uiVal=0))],
        "throttle": NVMLError(NO_PERMISSION),
    }])
    backend.init()
    (dev,) = backend.get_devices()
    assert dev.power_limit_max is None
    s = backend.sample(dev)
    assert s.power_limit is None and s.fan is None
    assert s.pcie_tx is None and s.pcie_rx is None
    assert s.enc_util is None and s.temp_mem is None
    assert s.throttle_reasons is None
    assert s.power_draw == 178.0  # still reported


def test_power_limit_fallback(nvml):
    backend, _ = nvml([{"limit": None, "limit_fallback": 150_000}])
    backend.init()
    (dev,) = backend.get_devices()
    assert backend.sample(dev).power_limit == 150.0


def test_missing_function_in_old_bindings(nvml):
    backend, mod = nvml([{}])
    del mod.nvmlDeviceGetCurrentClocksEventReasons
    mod.nvmlDeviceGetCurrentClocksThrottleReasons = lambda h: 0x1
    del mod.nvmlDeviceGetFieldValues
    backend.init()
    (dev,) = backend.get_devices()
    s = backend.sample(dev)
    assert s.throttle_reasons == ("idle",)
    assert s.temp_mem is None


def test_processes_merge_compute_and_graphics(nvml):
    backend, _ = nvml([{
        "compute": [NS(pid=100, usedGpuMemory=2 << 30), NS(pid=200, usedGpuMemory=1 << 20)],
        "graphics": [NS(pid=200, usedGpuMemory=3 << 20), NS(pid=300, usedGpuMemory=None),
                     NS(pid=400, usedGpuMemory=2**64 - 1)],
        "proc_util": [NS(pid=100, smUtil=70, timeStamp=10), NS(pid=100, smUtil=90, timeStamp=20)],
    }])
    backend.init()
    (dev,) = backend.get_devices()
    procs = {p.pid: p for p in backend.get_processes(dev)}
    assert procs[100].type == "Compute" and procs[100].vram_used == 2 << 30
    assert procs[200].type == "C+G" and procs[200].vram_used == 3 << 20
    assert procs[300].type == "Graphics" and procs[300].vram_used is None
    assert procs[400].vram_used is None  # NVML_VALUE_NOT_AVAILABLE
    assert procs[100].sm_util == 90  # max over the samples since last query
    assert procs[200].sm_util == 0.0  # supported but idle -> 0, not "-"


def test_process_utilisation_not_found_means_zero(nvml):
    backend, _ = nvml([{"compute": [NS(pid=1, usedGpuMemory=1)]}])  # proc_util -> NOT_FOUND
    backend.init()
    (dev,) = backend.get_devices()
    assert backend.get_processes(dev)[0].sm_util == 0.0


def test_process_utilisation_unsupported_is_none(nvml):
    backend, _ = nvml([{"compute": [NS(pid=1, usedGpuMemory=1)], "proc_util": None}])
    backend.init()
    (dev,) = backend.get_devices()
    assert backend.get_processes(dev)[0].sm_util is None


def test_gpu_lost_raises_device_lost(nvml):
    backend, mod = nvml([{}, {}])
    backend.init()
    d0, d1 = backend.get_devices()
    mod.state["lost"].add(1)
    backend.sample(d0)
    with pytest.raises(DeviceLostError):
        backend.sample(d1)
    with pytest.raises(DeviceLostError):
        backend.get_processes(d1)


def test_gpu_lost_in_sampler_keeps_other_gpus(nvml):
    backend, mod = nvml([{}, {}])
    backend.init()
    sampler = Sampler(backend, resolve_processes=False)
    sampler.discover()
    sampler.poll_once()
    mod.state["lost"].add(0)
    sampler.poll_once()
    gpu0, gpu1 = sampler.devices
    assert gpu0.lost and not gpu1.lost
    assert len(gpu1.history["util_gpu"]) == 2


@pytest.mark.parametrize("code,needle", [
    (LIBRARY_NOT_FOUND, "libnvidia-ml.so"),
    (DRIVER_NOT_LOADED, "kernel driver is not loaded"),
    (NO_PERMISSION, "permission"),
    (999, "could not initialise NVML"),
])
def test_init_errors_are_explained(nvml, code, needle):
    backend, _ = nvml([{}], fail_init=code)
    with pytest.raises(BackendError, match=needle):
        backend.init()


def test_shutdown_is_idempotent(nvml):
    backend, mod = nvml([{}])
    backend.init()
    backend.shutdown()
    backend.shutdown()
    assert mod.state["shutdown"] == 1
