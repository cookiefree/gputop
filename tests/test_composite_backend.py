"""``--backend auto``: several backends side by side, renumbered."""

import pytest

from fake_backend import ScriptedBackend
from fake_sysfs import FakeSysfs, drm_fdinfo
from gputop.backends import AUTO_BACKENDS, create_backend
from gputop.backends.base import BackendError, DeviceLostError, GPUBackend, GPUProcess
from gputop.backends.composite import CompositeBackend
from gputop.backends.intel import IntelBackend
from gputop.sampler import Sampler


class Failing(GPUBackend):
    name = "failing"

    def init(self):
        raise BackendError("library not found")

    def get_devices(self):
        raise AssertionError("not reached")

    sample = get_processes = get_devices


class Empty(ScriptedBackend):
    def __init__(self):
        super().__init__(count=0)


def test_auto_includes_real_backends_only():
    assert AUTO_BACKENDS == ("nvidia", "intel")
    assert isinstance(create_backend("auto"), CompositeBackend)


def test_devices_are_renumbered_across_backends():
    a = ScriptedBackend(count=2, processes={1: [GPUProcess(pid=7, gpu_index=1, user="u", command="c")]})
    b = ScriptedBackend(count=1, util=lambda i, t: 42.0)
    comp = CompositeBackend([("a", lambda: a), ("broken", Failing), ("none", Empty), ("b", lambda: b)])
    comp.init()
    devs = comp.get_devices()
    assert [d.index for d in devs] == [0, 1, 2]
    assert [d.name for d in devs] == ["Scripted GPU 0", "Scripted GPU 1", "Scripted GPU 0"]
    assert comp.label == "a+b"
    assert comp.skipped == {"broken": "library not found", "none": "no devices"}

    s = comp.sample(devs[2])
    assert s.index == 2 and s.util_gpu == 42.0
    (p,) = comp.get_processes(devs[1])
    assert p.pid == 7 and p.gpu_index == 1
    assert comp.supports_processes(devs[0])
    comp.shutdown()
    assert a.shutdown_called and b.shutdown_called


def test_child_device_loss_is_reported_with_global_index():
    a = ScriptedBackend(count=1)
    b = ScriptedBackend(count=1, lose={0: 2})
    comp = CompositeBackend([("a", lambda: a), ("b", lambda: b)])
    comp.init()
    devs = comp.get_devices()
    comp.sample(devs[1])
    with pytest.raises(DeviceLostError) as exc:
        comp.sample(devs[1])
    assert exc.value.index == 1


def test_no_usable_backend_lists_reasons():
    comp = CompositeBackend([("nvidia", Failing), ("intel", Empty)])
    with pytest.raises(BackendError) as exc:
        comp.init()
    assert "nvidia: library not found" in str(exc.value)
    assert "intel: no devices" in str(exc.value)


def test_backend_crash_does_not_stop_auto():
    def boom():
        raise RuntimeError("bug")

    comp = CompositeBackend([("buggy", boom), ("ok", lambda: ScriptedBackend(count=1))])
    comp.init()
    assert len(comp.get_devices()) == 1 and "buggy" in comp.skipped


def test_nvidia_and_intel_together(tmp_path):
    tree = FakeSysfs(tmp_path)
    tree.add_card(0, pci="0000:00:02.0")
    tree.add_proc(50, "kitty", ["kitty"], {3: ("/dev/dri/renderD128", drm_fdinfo())})
    nvidia = ScriptedBackend(count=1)
    intel = IntelBackend(root=tree.root, counter_opener=tree.open_counter, clock=tree.clock)
    comp = CompositeBackend([("nvidia", lambda: nvidia), ("intel", lambda: intel)])
    sampler = Sampler(comp, resolve_processes=False)
    sampler.discover()
    sampler.poll_once()
    nv, ig = sampler.devices
    assert (nv.index, ig.index) == (0, 1)
    assert ig.info.vendor == "Intel" and ig.latest.clock_graphics == 300
    assert [(p.pid, p.gpu_index) for p in ig.processes] == [(50, 1)]
