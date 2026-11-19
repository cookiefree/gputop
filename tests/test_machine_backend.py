"""--backend auto: every GPU on the machine, merged with live backends.

Scenarios from a fake sysfs tree (plus a fake NVML), including the critical
guarantee: no backend is ever queried about a GPU that is not awake.
"""

import sys

import pytest

from fake_backend import ScriptedBackend
from fake_nvml import make_pynvml
from fake_sysfs import FakeSysfs
from gputop import pcidiscovery as pci
from gputop.backends import build_backend
from gputop.backends.base import BackendError, GPUBackend, GPUInfo, GPUProcess, GPUSample
from gputop.backends.intel import IntelBackend
from gputop.backends.machine import MachineBackend
from gputop.sampler import Sampler

IGPU, DGPU, DGPU2 = "0000:00:02.0", "0000:01:00.0", "0000:02:00.0"


@pytest.fixture
def tree(tmp_path):
    t = FakeSysfs(tmp_path)
    t.add_pci_ids({0x9A49: "TigerLake-LP GT2 [Iris Xe Graphics]"}, nvidia={0x1F9D: "TU117M [GeForce GTX 1650 Mobile]"})
    return t


class Spy(GPUBackend):
    """A backend that records every call and which device it was about."""

    name = "spy"

    def __init__(self, devices: list[GPUInfo], log: list, fail: str | None = None) -> None:
        self.devices, self.log, self.fail = devices, log, fail

    def init(self):
        self.log.append(("init", None))
        if self.fail:
            raise BackendError(self.fail)

    def shutdown(self):
        self.log.append(("shutdown", None))

    def get_devices(self):
        self.log.append(("get_devices", None))
        return list(self.devices)

    def get_devices_for(self, addresses):
        addresses = list(addresses)
        self.log.append(("get_devices_for", tuple(addresses)))
        return [d for d in self.devices if d.pci_address in addresses]

    def sample(self, device):
        self.log.append(("sample", device.pci_address))
        return GPUSample(index=device.index, util_gpu=42.0)

    def get_processes(self, device):
        self.log.append(("get_processes", device.pci_address))
        return [GPUProcess(pid=1, gpu_index=device.index, user="u", command="c")]


def touched(log, address):
    return [c for c in log if c[1] == address or (isinstance(c[1], tuple) and address in c[1])]


def intel_factory(tree):
    return lambda: IntelBackend(root=tree.root, counter_opener=tree.open_counter, clock=tree.clock)


def machine(tree, backends, **kw):
    m = MachineBackend(backends, root=str(tree.root), **kw)
    m.init()
    return m


def poll(m, n=1):
    s = Sampler(m, resolve_processes=False)
    s.discover()
    for _ in range(n):
        s.poll_once()
    return s


# -- scenarios ------------------------------------------------------------------------


def test_intel_active_and_nvidia_suspended(tree):
    tree.add_card(0, pci=IGPU)
    tree.add_coretemp()
    tree.add_pci(DGPU, vendor="0x10de", device_id="0x1f9d", driver="nvidia",
                 runtime_status="suspended", power_state="D3cold")
    nv_log = []
    m = machine(tree, [("nvidia", lambda: Spy([], nv_log)), ("intel", intel_factory(tree))])
    s = poll(m)
    igpu, dgpu = s.devices
    assert igpu.latest.status == pci.ACTIVE and igpu.latest.live
    assert igpu.latest.clock_graphics == 300 and igpu.info.vendor == "Intel"
    assert dgpu.latest.status == pci.SUSPENDED and not dgpu.latest.live
    assert dgpu.latest.status_detail == "suspended (D3cold) - not queried to avoid waking it"
    assert dgpu.info.name == "NVIDIA TU117M [GeForce GTX 1650 Mobile]" and dgpu.info.pci_address == DGPU
    assert nv_log == []  # NVML not even initialised while the only NVIDIA GPU sleeps
    assert "not probed" in m.skipped["nvidia"]
    assert dgpu.processes == []


def test_nvidia_off_d3cold_no_driver(tree):
    tree.add_pci(DGPU, driver=None, enable=0, runtime_status="suspended", power_state="D3cold")
    m = machine(tree, [("nvidia", lambda: Spy([], []))])
    (dev,) = poll(m).devices
    assert dev.latest.status == pci.OFF
    assert dev.latest.status_detail == "GPU is off - no data"
    assert dev.latest.driver is None and dev.latest.power_state == "D3cold"


def test_nvidia_no_driver(tree):
    tree.add_pci(DGPU, driver=None, enable=0, runtime_status="active", power_state="D0")
    (dev,) = poll(machine(tree, [])).devices
    assert dev.latest.status == pci.NO_DRIVER and dev.latest.status_detail == "no driver loaded"


def test_nvidia_bound_to_nouveau(tree):
    tree.add_pci(DGPU, driver="nouveau")
    nv_log = []
    m = machine(tree, [("nvidia", lambda: Spy([], nv_log))])
    (dev,) = poll(m).devices
    assert dev.latest.status == pci.UNSUPPORTED
    assert dev.latest.status_detail == "driver nouveau - no stats available"
    assert nv_log == []  # nouveau isn't the nvidia driver: NVML is never tried


def test_switched_off_macbook_dgpu(tree):
    """The real state of this project's dev machine: nouveau, enable=0, D3hot."""
    tree.add_pci(DGPU, driver="nouveau", enable=0, runtime_status="active", power_state="D3hot")
    (dev,) = poll(machine(tree, [])).devices
    assert dev.latest.status == pci.OFF


def test_nvidia_active_with_fake_nvml(tree, monkeypatch):
    tree.add_card(0, pci=IGPU)
    tree.add_pci(DGPU, vendor="0x10de", device_id="0x1f9d", driver="nvidia")
    fake = make_pynvml([{"bus": "00000000:01:00.0", "name": "NVIDIA GeForce GTX 1650"}])
    monkeypatch.setitem(sys.modules, "pynvml", fake)
    from gputop.backends.nvidia import NvidiaBackend

    m = machine(tree, [("nvidia", NvidiaBackend), ("intel", intel_factory(tree))])
    s = poll(m)
    assert len(s.devices) == 2  # merged by PCI address, not listed twice
    igpu, dgpu = s.devices
    assert dgpu.latest.status == pci.ACTIVE and dgpu.latest.live
    assert dgpu.latest.util_gpu == 87.0 and dgpu.latest.mem_total == 12 << 30
    assert dgpu.info.name == "NVIDIA GeForce GTX 1650" and dgpu.info.pci_address == DGPU
    assert dgpu.info.pci_ids == "10de:1f9d"
    assert m.label == "nvidia+intel"
    assert ("nvmlDeviceGetHandleByPciBusId_v2", DGPU) in fake.calls


def test_nothing_found(tree):
    m = MachineBackend([("nvidia", lambda: Spy([], [], fail="NVML library not found")),
                        ("intel", lambda: Spy([], [], fail="no Intel GPU found")),
                        ("amd", lambda: Spy([], [], fail="not implemented"))], root=str(tree.root))
    with pytest.raises(BackendError) as exc:
        m.init()
    msg = str(exc.value)
    assert msg.splitlines()[0] == "no GPU found"
    assert "nvidia: NVML library not found" in msg and "intel: no Intel GPU found" in msg
    assert "amd: not implemented" in msg and "--backend fake" in msg


def test_failing_backend_is_skipped_in_auto_mode(tree):
    tree.add_pci(DGPU, driver="nvidia")
    m = machine(tree, [("nvidia", lambda: Spy([], [], fail="NVML library not found"))])
    (dev,) = poll(m).devices
    assert dev.latest.status == pci.UNSUPPORTED
    assert "NVML library not found" in dev.latest.status_detail


def test_forced_backend_failure_is_an_error(tree):
    tree.add_pci(DGPU, driver="nvidia")
    m = MachineBackend([("nvidia", lambda: Spy([], [], fail="NVML library not found"))],
                       root=str(tree.root), forced=True)
    with pytest.raises(BackendError, match="NVML"):
        m.init()


def test_forced_backend_with_sleeping_gpu_is_not_woken(tree):
    tree.add_pci(DGPU, driver="nvidia", runtime_status="suspended", power_state="D3cold")
    log = []
    m = machine(tree, [("nvidia", lambda: Spy([], log))], forced=True)
    (dev,) = poll(m).devices
    assert dev.latest.status == pci.SUSPENDED and log == []


def test_no_pci_mode_shows_only_backend_devices(tree):
    tree.add_pci(DGPU, driver=None)
    m = machine(tree, [("x", lambda: ScriptedBackend(count=2))], use_pci=False)
    devs = poll(m).devices
    assert [d.info.name for d in devs] == ["Scripted GPU 0", "Scripted GPU 1"]
    assert all(d.latest.status == pci.ACTIVE for d in devs)


# -- the non-waking guarantee ---------------------------------------------------------------


def test_no_backend_query_for_suspended_or_off_devices(tree):
    """Across startup, polling, process queries, wake and sleep transitions,
    no backend call ever concerns a GPU that isn't awake."""
    tree.add_pci(DGPU, driver="nvidia")  # awake
    tree.add_pci(DGPU2, driver="nvidia", runtime_status="suspended", power_state="D3cold")
    tree.add_pci("0000:03:00.0", driver="nvidia", enable=0, power_state="D3cold")  # off
    devices = [GPUInfo(index=i, name=f"g{i}", pci_address=a) for i, a in enumerate([DGPU, DGPU2, "0000:03:00.0"])]
    log = []
    m = machine(tree, [("nvidia", lambda: Spy(devices, log))])
    s = poll(m, 5)
    for dev in s.devices:
        m.supports_processes(dev.info)
    assert ("get_devices", None) not in log  # never a blanket enumeration
    assert touched(log, DGPU2) == [] and touched(log, "0000:03:00.0") == []
    assert ("sample", DGPU) in log and ("get_processes", DGPU) in log
    assert [d.latest.status for d in s.devices] == [pci.ACTIVE, pci.SUSPENDED, pci.OFF]
    assert s.devices[0].processes and not s.devices[1].processes


def test_wake_and_sleep_transitions(tree):
    tree.add_pci(DGPU, driver="nvidia", runtime_status="suspended", power_state="D3cold")
    dev_info = GPUInfo(index=0, name="dgpu", pci_address=DGPU)
    log = []
    m = machine(tree, [("nvidia", lambda: Spy([dev_info], log))])
    s = Sampler(m, resolve_processes=False)
    s.discover()
    for _ in range(3):
        s.poll_once()
    assert log == [] and len(s.devices[0].history["util_gpu"]) == 0

    tree.set_power(DGPU, runtime_status="active", power_state="D0")  # the GPU wakes up
    for _ in range(3):
        s.poll_once()
    assert ("init", None) in log and ("sample", DGPU) in log
    assert s.devices[0].latest.status == pci.ACTIVE
    assert s.devices[0].history["util_gpu"].values() == [42.0, 42.0, 42.0]

    tree.set_power(DGPU, runtime_status="suspended", power_state="D3cold")  # back to sleep
    n = len(log)
    for _ in range(3):
        s.poll_once()
    assert log[n:] == [("shutdown", None)]  # released, then left alone
    assert s.devices[0].latest.status == pci.SUSPENDED
    # history froze while it slept (no gaps appended)
    assert s.devices[0].history["util_gpu"].values() == [42.0, 42.0, 42.0]


def test_intel_backend_never_reads_a_sleeping_card(tree, monkeypatch):
    """Intel iGPU awake + Intel dGPU suspended: none of the dGPU's sensor,
    frequency or energy files is read (only PCI identity attributes)."""
    import gputop.backends.intel as intel_mod

    tree.add_card(0, pci=IGPU)
    dgpu_dev = tree.add_card(1, pci="0000:03:00.0", device_id="0x56a0")
    tree.add_card_hwmon("0000:03:00.0", temp_mc=48000, energy_uj=1)
    tree.set_power("0000:03:00.0", runtime_status="suspended", power_state="D3cold")
    tree.add_pmu(name="i915_0000_03_00.0", engines={"rcs0": 0x9})
    reads = []
    real_read = intel_mod._read
    monkeypatch.setattr(intel_mod, "_read", lambda p: reads.append(str(p)) or real_read(p))
    m = machine(tree, [("intel", intel_factory(tree))])
    s = poll(m, 3)
    assert [d.latest.status for d in s.devices] == [pci.ACTIVE, pci.SUSPENDED]
    card1 = str(tree.root / "sys/class/drm/card1")
    allowed = {"vendor", "class", "device"}
    bad = [r for r in reads if (r.startswith(card1) or r.startswith(str(dgpu_dev)))
           and r.rsplit("/", 1)[-1] not in allowed]
    assert bad == []
    assert 0x9 not in [c.config for c in tree.counters]  # dGPU PMU never opened


def test_history_resumes_after_wake_with_real_backend(tree):
    tree.add_card(0, pci=IGPU)
    m = machine(tree, [("intel", intel_factory(tree))])
    s = poll(m, 2)
    assert len(s.devices[0].history["clock_graphics"]) == 2
    tree.set_power(IGPU, runtime_status="suspended", power_state="D3hot")
    s.poll_once()
    assert s.devices[0].latest.status == pci.SUSPENDED
    assert len(s.devices[0].history["clock_graphics"]) == 2
    tree.set_power(IGPU, runtime_status="active", power_state="D0")
    s.poll_once()
    assert s.devices[0].latest.status == pci.ACTIVE
    assert len(s.devices[0].history["clock_graphics"]) == 3


def test_gpu_removed_from_bus_is_lost(tree):
    import shutil

    tree.add_pci(DGPU, driver=None)
    s = poll(machine(tree, []))
    shutil.rmtree(tree.root / "sys/devices/pci0000:00" / DGPU)
    (tree.root / "sys/bus/pci/devices" / DGPU).unlink()
    s.poll_once()
    assert s.devices[0].lost


def test_rediscovery_picks_up_new_gpus_and_keeps_history(tree):
    tree.add_card(0, pci=IGPU)
    m = machine(tree, [("intel", intel_factory(tree))])
    s = poll(m, 2)
    tree.add_pci(DGPU, driver=None)  # hot-plugged
    assert s.rediscover() is True
    s.poll_once()
    assert [d.info.pci_address for d in s.devices] == [IGPU, DGPU]
    assert len(s.devices[0].history["clock_graphics"]) == 3  # kept across rediscovery
    assert s.devices[1].latest.status == pci.NO_DRIVER
    assert s.rediscover() is False


def test_build_backend_specs(tree):
    from gputop.backends import parse_backend_spec

    assert parse_backend_spec("nvidia,intel") == ["nvidia", "intel"]
    with pytest.raises(ValueError):
        parse_backend_spec("nvidia,bogus")
    with pytest.raises(ValueError):
        parse_backend_spec("auto,nvidia")
    fake = build_backend("fake", fake_count=3)
    assert not fake.use_pci  # synthetic GPUs never mix with the real machine's
    assert len(fake.get_devices()) == 3
