"""A mocked ``pynvml`` module (no NVIDIA driver needed).

Every NVML function records ``(name, handle)`` in ``module.calls`` so tests can
assert which GPUs were (and were not) queried.
"""

import types
from types import SimpleNamespace as NS

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
    m.nvmlDeviceGetPciInfo = lambda h: NS(busId=get(h, "bus", f"00000000:{h + 1:02X}:00.0").encode())

    def by_bus(bus_id):
        want = bus_id.lower()[-7:]  # "01:00.0", tolerant of 4/8-digit domains
        for i, g in enumerate(gpus):
            if g.get("bus", f"00000000:{i + 1:02X}:00.0").lower().endswith(want):
                if i in m.state["lost"]:
                    raise NVMLError(GPU_IS_LOST)
                return i
        raise NVMLError(NOT_FOUND)

    m.nvmlDeviceGetHandleByPciBusId_v2 = by_bus

    # Record every call (name, first argument = handle / bus id) for assertions.
    m.calls = []
    for name in [n for n in dir(m) if n.startswith("nvml")]:
        fn = getattr(m, name)
        if callable(fn):
            def wrapper(*args, _fn=fn, _name=name):
                m.calls.append((_name, args[0] if args else None))
                return _fn(*args)
            setattr(m, name, wrapper)
    return m


