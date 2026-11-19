"""Intel backend tests against a fake sysfs/procfs tree (no GPU, no root)."""

import errno
import os
import shutil

import pytest

from fake_sysfs import FakeSysfs, drm_fdinfo
from gputop.backends.base import BackendError, DeviceLostError
from gputop.backends.intel import IntelBackend
from gputop.sampler import Sampler

IGPU = "0000:00:02.0"
DGPU = "0000:03:00.0"


@pytest.fixture
def tree(tmp_path):
    return FakeSysfs(tmp_path)


def make(tree: FakeSysfs) -> IntelBackend:
    b = IntelBackend(root=tree.root, counter_opener=tree.open_counter, clock=tree.clock)
    b.init()
    return b


def igpu(tree: FakeSysfs, **kwargs) -> FakeSysfs:
    """A typical laptop: NVIDIA card0 (ignored) + Intel iGPU card1."""
    tree.add_card(0, pci="0000:01:00.0", vendor="0x10de", device_id="0x0fe9", driver="nouveau")
    tree.add_card(1, pci=IGPU, **kwargs)
    tree.add_coretemp()
    tree.add_pci_ids({0x9A49: "TigerLake-LP GT2 [Iris Xe Graphics]"})
    return tree


# -- discovery -------------------------------------------------------------------


def test_discovers_only_intel_display_cards(tree):
    b = make(igpu(tree))
    (dev,) = b.get_devices()
    assert dev.index == 0
    assert dev.name == "Intel TigerLake-LP GT2 [Iris Xe Graphics]"
    assert dev.uuid == IGPU and dev.short_uuid == IGPU
    assert dev.vendor == "Intel"
    assert dev.driver_version == "i915 6.9.0-test"
    assert dev.memory_total is None  # shared memory: no VRAM
    assert dev.clock_graphics_max == 1300


def test_multiple_intel_cards_numbered_in_order(tree):
    tree.add_card(0, pci=IGPU)
    tree.add_card(1, pci=DGPU, device_id="0x56a0")
    tree.add_card(10, pci="0000:04:00.0", device_id="0x56a1")
    b = make(tree)
    assert [(d.index, d.uuid) for d in b.get_devices()] == [(0, IGPU), (1, DGPU), (2, "0000:04:00.0")]


def test_name_fallback_without_pci_ids(tree):
    tree.add_card(0, pci=IGPU, device_id="0x46a6")
    assert make(tree).get_devices()[0].name == "Intel GPU [8086:46a6]"


def test_no_intel_gpu_is_a_backend_error(tree):
    tree.add_card(0, pci="0000:01:00.0", vendor="0x10de")
    with pytest.raises(BackendError, match="no Intel GPU"):
        make(tree)


def test_missing_drm_dir_is_a_backend_error(tmp_path):
    with pytest.raises(BackendError):
        IntelBackend(root=tmp_path / "nothing").init()


# -- frequency -------------------------------------------------------------------


@pytest.mark.parametrize("style", ["card", "gt", "xe"])
def test_frequency_sources(tree, style):
    tree.add_card(0, pci=IGPU, freq={"act": 450, "cur": 500, "max": 1100}, freq_style=style)
    b = make(tree)
    (dev,) = b.get_devices()
    assert dev.clock_graphics_max == 1100
    assert b.sample(dev).clock_graphics == 450  # actual preferred over requested


def test_frequency_falls_back_to_requested(tree):
    tree.add_card(0, pci=IGPU, freq={"cur": 500, "max": 1100})
    b = make(tree)
    assert b.sample(b.get_devices()[0]).clock_graphics == 500


def test_no_frequency_files(tree):
    tree.add_card(0, pci=IGPU, freq={})
    b = make(tree)
    (dev,) = b.get_devices()
    assert dev.clock_graphics_max is None
    assert b.sample(dev).clock_graphics is None


# -- utilisation (perf PMU) --------------------------------------------------------


def test_engine_busy_deltas(tree):
    igpu(tree).add_pmu()
    b = make(tree)
    (dev,) = b.get_devices()
    assert len(tree.counters) == 3  # one counter per *-busy event, not -wait/-sema
    tree.clock.advance(1.0)
    tree.pmu_values = {0x0: 500_000_000, 0x100000: 0, 0x200000: 250_000_000}
    s = b.sample(dev)
    assert s.util_gpu == 50.0
    assert s.engine_util == {"copy": 0.0, "render": 50.0, "video": 25.0}
    assert s.notes == ()
    tree.clock.advance(0.5)
    tree.pmu_values[0x0] += 500_000_000  # fully busy for the last 0.5 s
    assert b.sample(dev).util_gpu == 100.0
    b.shutdown()
    assert all(c.closed for c in tree.counters)


def test_util_from_video_engine_when_no_render_engine(tree):
    igpu(tree).add_pmu(engines={"vcs0": 0x200000, "vcs1": 0x200001})
    b = make(tree)
    tree.clock.advance(1.0)
    tree.pmu_values = {0x200000: 100_000_000, 0x200001: 300_000_000}
    s = b.sample(b.get_devices()[0])
    assert s.util_gpu == 30.0
    assert set(s.engine_util) == {"video", "video1"}


@pytest.mark.parametrize("err", [errno.EACCES, errno.EPERM])
def test_pmu_permission_denied_hides_util_with_hint(tree, err):
    igpu(tree).add_pmu()
    tree.pmu_error = err
    b = make(tree)
    s = b.sample(b.get_devices()[0])
    assert s.util_gpu is None and s.engine_util is None
    (note,) = s.notes
    assert "perf_event_paranoid=0" in note and "(now 2)" in note and "CAP_PERFMON" in note


def test_pmu_unavailable_hides_util_silently(tree):
    igpu(tree).add_pmu()
    tree.pmu_error = errno.ENOENT
    b = make(tree)
    s = b.sample(b.get_devices()[0])
    assert s.util_gpu is None and s.notes == ()


def test_no_pmu_at_all(tree):
    b = make(igpu(tree))
    s = b.sample(b.get_devices()[0])
    assert s.util_gpu is None and s.notes == ()


def test_discrete_card_uses_its_own_named_pmu(tree):
    tree.add_card(0, pci=DGPU)
    tree.add_pmu(name="i915")  # belongs to the iGPU, must not be used
    tree.add_pmu(name="i915_0000_03_00.0", engines={"rcs0": 0x7})
    make(tree)
    assert [c.config for c in tree.counters] == [0x7]


# -- power -----------------------------------------------------------------------


def test_power_prefers_gpu_rapl_domain(tree):
    igpu(tree)
    tree.add_rapl("intel-rapl:0", "package-0", 5_000_000, limit_uw=28_000_000)
    gpu = tree.add_rapl("intel-rapl:0:1", "uncore", 1_000_000, limit_uw=0)
    b = make(tree)
    (dev,) = b.get_devices()
    assert b.sample(dev).power_draw is None  # first reading only primes the delta
    tree.clock.advance(2.0)
    tree.set_energy(gpu, 1_000_000 + 7_000_000)  # 7 J over 2 s
    s = b.sample(dev)
    assert s.power_draw == 3.5
    assert s.power_limit is None  # uncore has no limit
    assert not any("package" in n for n in s.notes)


def test_power_falls_back_to_package_with_note(tree):
    igpu(tree)
    pkg = tree.add_rapl("intel-rapl:0", "package-0", 0, limit_uw=28_000_000)
    b = make(tree)
    (dev,) = b.get_devices()
    assert dev.power_limit_max == 28.0
    b.sample(dev)
    tree.clock.advance(1.0)
    tree.set_energy(pkg, 12_000_000)
    s = b.sample(dev)
    assert s.power_draw == 12.0 and s.power_limit == 28.0
    assert any("CPU package" in n for n in s.notes)


def test_power_counter_wraparound(tree):
    igpu(tree)
    gpu = tree.add_rapl("intel-rapl:0:1", "uncore", 999_000_000, max_range=999_999_999)
    b = make(tree)
    (dev,) = b.get_devices()
    b.sample(dev)
    tree.clock.advance(1.0)
    tree.set_energy(gpu, 1_000_000)  # wrapped: 999_999 + 1 + 1_000_000 µJ = 2 J
    assert b.sample(dev).power_draw == 2.0


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 files")
def test_unreadable_rapl_is_hidden(tree):
    igpu(tree)
    tree.add_rapl("intel-rapl:0:1", "uncore", 1, readable=False)
    tree.add_rapl("intel-rapl:0", "package-0", 1, readable=False)
    b = make(tree)
    s = b.sample(b.get_devices()[0])
    assert s.power_draw is None and s.power_limit is None
    assert s.notes == ()


# -- temperature / misc -------------------------------------------------------------


def test_igpu_temperature_from_coretemp_package(tree):
    b = make(igpu(tree))
    s = b.sample(b.get_devices()[0])
    assert s.temp_core == 61.0
    assert s.mem_used is None and s.mem_total is None
    assert s.fan is None and s.pcie_gen is None


def test_discrete_card_hwmon_and_pcie_link(tree):
    tree.add_card(0, pci=DGPU, device_id="0x56a0")
    tree.add_coretemp()  # must not be used for a discrete card
    hw = tree.add_card_hwmon(DGPU, temp_mc=48000, energy_uj=10_000_000, power_max_uw=225_000_000)
    tree.set_link(DGPU, cur="8.0 GT/s PCIe", width=8)
    b = make(tree)
    (dev,) = b.get_devices()
    assert dev.pcie_gen_max == 4 and dev.pcie_width_max == 16
    assert dev.power_limit_max == 225.0
    b.sample(dev)
    tree.clock.advance(1.0)
    tree.set_energy(hw, 10_000_000 + 95_000_000, file="energy1_input")
    s = b.sample(dev)
    assert s.temp_core == 48.0
    assert s.power_draw == 95.0 and s.power_limit == 225.0
    assert (s.pcie_gen, s.pcie_width) == (3, 8)


def test_throttle_reasons(tree):
    igpu(tree, throttle={"pl1": 1, "pl2": 1, "thermal": 0, "prochot": 1})
    b = make(tree)
    assert b.sample(b.get_devices()[0]).throttle_reasons == ("sw_power", "hw_thermal")


def test_no_throttle_files_means_unknown(tree):
    b = make(igpu(tree))
    assert b.sample(b.get_devices()[0]).throttle_reasons is None


def test_garbage_values_are_hidden(tree):
    igpu(tree)
    tree.write("sys/class/drm/card1/gt_act_freq_mhz", "garbage")
    tree.write("sys/class/drm/card1/gt_cur_freq_mhz", "")
    tree.write("sys/class/hwmon/hwmon4/temp1_input", "n/a")
    b = make(tree)
    s = b.sample(b.get_devices()[0])
    assert s.clock_graphics is None
    assert s.temp_core == 55.0  # falls back to the next readable coretemp sensor


# -- device loss -------------------------------------------------------------------


def test_card_disappearing_raises_device_lost(tree):
    b = make(igpu(tree))
    (dev,) = b.get_devices()
    b.sample(dev)
    shutil.rmtree(tree.root / "sys/class/drm/card1")
    with pytest.raises(DeviceLostError):
        b.sample(dev)
    with pytest.raises(DeviceLostError):
        b.get_processes(dev)


def test_sampler_marks_intel_device_lost(tree):
    b = make(igpu(tree))
    s = Sampler(b, resolve_processes=False)
    s.discover()
    s.poll_once()
    shutil.rmtree(tree.root / "sys/class/drm/card1")
    s.poll_once()
    assert s.devices[0].lost


# -- processes (fdinfo) --------------------------------------------------------------


def test_fdinfo_processes(tree):
    igpu(tree)
    tree.add_proc(100, "kwin_wayland", ["/usr/bin/kwin_wayland", "--x11"], {
        5: ("/dev/dri/card1", drm_fdinfo(client_id=1, engines={"render": 0})),
        6: ("/dev/dri/card1", drm_fdinfo(client_id=1, engines={"render": 0})),  # dup of client 1
    })
    tree.add_proc(200, "ffmpeg", ["ffmpeg", "-hwaccel", "vaapi"], {
        3: ("/dev/dri/renderD128", drm_fdinfo(client_id=2, engines={"render": 0, "video": 0},
                                              capacity={"video": 2})),
        4: ("/dev/null", None),
        7: ("socket:[1234]", None),
    })
    tree.add_proc(300, "nvidia-app", ["app"], {  # another GPU's client
        3: ("/dev/dri/renderD129", drm_fdinfo(pci="0000:01:00.0", client_id=9, driver="nouveau")),
    })
    tree.add_proc(400, "plain", ["plain"], {3: ("/dev/dri/renderD128", "pos:\t0\n")})  # no drm-* keys
    b = make(tree)
    (dev,) = b.get_devices()
    assert b.supports_processes(dev)

    first = {p.pid: p for p in b.get_processes(dev)}
    assert set(first) == {100, 200}
    assert first[100].sm_util == 0.0  # engine stats exist but no delta yet
    assert first[100].command == "/usr/bin/kwin_wayland --x11"
    assert first[100].user not in ("", "?")
    assert first[100].vram_used is None  # system memory is not VRAM

    tree.clock.advance(1.0)
    for fd in (5, 6):  # dup'ed fds of one client show the same fdinfo
        tree.write(f"proc/100/fdinfo/{fd}", drm_fdinfo(client_id=1, engines={"render": 200_000_000}))
    tree.write("proc/200/fdinfo/3", drm_fdinfo(client_id=2, engines={"render": 0, "video": 1_200_000_000},
                                               capacity={"video": 2}))
    procs = {p.pid: p for p in b.get_processes(dev)}
    assert procs[100].sm_util == 20.0 and procs[100].type == "Graphics"
    assert procs[200].sm_util == 60.0  # 1.2 s busy over 1 s on 2 video engines
    assert all(p.gpu_index == 0 for p in procs.values())


def test_compute_client_type(tree):
    igpu(tree)
    tree.add_proc(10, "python", ["python", "infer.py"], {
        3: ("/dev/dri/renderD128", drm_fdinfo(engines={"compute": 0, "render": 0})),
    })
    b = make(tree)
    (dev,) = b.get_devices()
    b.get_processes(dev)
    tree.clock.advance(1.0)
    tree.write("proc/10/fdinfo/3", drm_fdinfo(engines={"compute": 500_000_000, "render": 0}))
    (p,) = b.get_processes(dev)
    assert p.type == "Compute" and p.sm_util == 50.0


def test_fdinfo_without_engine_keys_has_no_util(tree):
    igpu(tree)
    tree.add_proc(10, "kitty", ["kitty"], {3: ("/dev/dri/renderD128", drm_fdinfo())})
    b = make(tree)
    (p,) = b.get_processes(b.get_devices()[0])
    assert p.sm_util is None and p.vram_used is None


def test_discrete_client_vram(tree):
    tree.add_card(0, pci=DGPU)
    tree.add_proc(10, "game", ["game"], {3: ("/dev/dri/renderD128", drm_fdinfo(pci=DGPU, local_kib=2048))})
    b = make(tree)
    (p,) = b.get_processes(b.get_devices()[0])
    assert p.vram_used == 2048 * 1024


def test_process_exiting_between_scan_and_read(tree):
    igpu(tree)
    base = tree.add_proc(10, "short", ["short"], {3: ("/dev/dri/renderD128", drm_fdinfo())})
    b = make(tree)  # init scans and finds pid 10
    shutil.rmtree(base)
    assert b.get_processes(b.get_devices()[0]) == []


def test_no_fdinfo_clients_means_processes_unsupported(tree):
    igpu(tree)
    tree.add_proc(10, "cat", ["cat"], {1: ("/dev/null", None)})
    b = make(tree)
    (dev,) = b.get_devices()
    assert not b.supports_processes(dev)
    assert b.get_processes(dev) == []


def test_new_clients_found_after_rescan_interval(tree):
    igpu(tree)
    b = make(tree)
    (dev,) = b.get_devices()
    assert not b.supports_processes(dev)
    tree.add_proc(10, "late", ["late"], {3: ("/dev/dri/renderD128", drm_fdinfo())})
    assert b.get_processes(dev) == []  # within the scan interval: not yet seen
    tree.clock.advance(6.0)
    assert [p.pid for p in b.get_processes(dev)] == [10]
    assert b.supports_processes(dev)


def test_unreadable_proc_entries_are_skipped(tree):
    igpu(tree)
    base = tree.add_proc(10, "other-user", ["x"], {3: ("/dev/dri/renderD128", drm_fdinfo())})
    (base / "fd").chmod(0)
    try:
        b = make(tree)
        if os.geteuid() != 0:
            assert b.get_processes(b.get_devices()[0]) == []
    finally:
        (base / "fd").chmod(0o755)


# -- read-only guarantee ---------------------------------------------------------------


def test_backend_never_writes_to_the_tree(tree):
    igpu(tree, throttle={"pl1": 0}).add_pmu()
    tree.add_rapl("intel-rapl:0:1", "uncore", 1)
    tree.add_proc(10, "kitty", ["kitty"], {3: ("/dev/dri/renderD128", drm_fdinfo())})

    def snapshot():
        out = {}
        for dirpath, dirnames, filenames in os.walk(tree.root):
            for name in filenames + dirnames:
                p = os.path.join(dirpath, name)
                st = os.lstat(p)
                out[p] = (st.st_mode, st.st_size, st.st_mtime_ns)
        return out

    before = snapshot()
    b = make(tree)
    (dev,) = b.get_devices()
    for _ in range(3):
        tree.clock.advance(1.0)
        b.sample(dev)
        b.get_processes(dev)
    b.shutdown()
    assert snapshot() == before
