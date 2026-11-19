"""End-to-end UI tests: run the Textual app headless on the fake backend."""

import asyncio

import pytest

from fake_backend import make_fake
from gputop.app import GpuTopApp, overview_layout
from gputop.sampler import Sampler
from gputop.themes import available_themes
from gputop.widgets.gpu_panel import GpuDetail, GpuPanel
from gputop.widgets.help_overlay import HelpOverlay
from gputop.widgets.process_table import ProcessTable


def screen_text(app) -> str:
    strips = app.screen._compositor.render_strips()
    return "\n".join("".join(seg.text for seg in strip) for strip in strips)


def make_app(count=2, warmup=40, **fake_kwargs) -> GpuTopApp:
    sampler = Sampler(make_fake(count=count, **fake_kwargs), interval=0.1)
    sampler.discover()
    for _ in range(warmup):
        sampler.poll_once()
    return GpuTopApp(sampler, available_themes(None), "default", backend_label="fake")


def run(app, scenario, size=(100, 30)):
    async def main():
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.3)
            await scenario(pilot, app)

    asyncio.run(main())


def test_overview_renders_all_gpus_and_processes():
    async def scenario(pilot, app):
        text = screen_text(app)
        assert "┐¹gpu0┌┐RTX 4090┌" in text and "┐²gpu1┌┐RTX 3060┌" in text
        assert "util" in text and "vram" in text
        assert "Pid:" in text and "Command:" in text and "┐proc┌" in text
        assert any(ch in text for ch in "⣀⣄⣤⣦⣶⣷⣿")
        assert len(app.query(GpuPanel)) == 2
        assert app.table.rows, "process table should be populated"

    run(make_app(), scenario)


def test_toggle_detail_view_filters_processes():
    async def scenario(pilot, app):
        await pilot.press("tab")
        assert app.focus_pos == 1
        await pilot.press("a")
        await pilot.pause(0.2)
        assert app.detail_mode
        assert len(app.query(GpuDetail)) == 1
        assert app.table.gpu_filter == 1
        assert all(p.gpu_index == 1 for p in app.table.rows)
        assert "┐gpu1 only┌" in screen_text(app)
        await pilot.press("shift+tab")  # switches the detail GPU too
        await pilot.pause(0.2)
        assert app.table.gpu_filter == 0
        await pilot.press("a")
        await pilot.pause(0.2)
        assert not app.detail_mode
        assert len(app.query(GpuDetail)) == 0
        assert app.table.gpu_filter is None

    run(make_app(count=3), scenario)


def test_number_keys_focus_gpus():
    async def scenario(pilot, app):
        await pilot.press("3")
        assert app.focus_pos == 2
        assert app.panels[2].selected and not app.panels[0].selected
        await pilot.press("9")  # out of range: ignored
        assert app.focus_pos == 2
        await pilot.press("tab")
        assert app.focus_pos == 0  # wraps

    run(make_app(count=3), scenario)


def test_filter_typing_does_not_trigger_shortcuts():
    async def scenario(pilot, app):
        await pilot.press("f")
        table = app.table
        assert table.editing and table.has_focus
        assert "┐filter: █┌" in screen_text(app)  # typed into the title, like btop
        # 'q', 'a' and 't' are app shortcuts; while typing they must be text.
        for ch in "qat":
            await pilot.press(ch)
        assert app.is_running and not app.detail_mode
        assert table.filter_text == "qat"
        assert table.rows == []
        assert "┐filter: qat█┌" in screen_text(app)
        assert "no matching processes" in screen_text(app)
        await pilot.press("backspace")
        assert table.filter_text == "qa"
        await pilot.press("escape")  # clears and closes, does not quit
        await pilot.pause(0.1)
        assert app.is_running
        assert table.filter_text == "" and not table.editing and not table.has_focus
        assert table.rows

        await pilot.press("f", "p", "y", "enter")
        assert table.filter_text == "py" and not table.editing
        assert "┐filter: py┌" in screen_text(app)
        assert all("py" in p.command.lower() or "py" in p.user for p in table.rows)
        await pilot.press("q")  # shortcuts work again after Enter
        await pilot.pause(0.1)
        assert not app.is_running

    run(make_app(count=3), scenario)


def test_sort_keys_and_header_click():
    async def scenario(pilot, app):
        table = app.query_one(ProcessTable)
        assert table.sort_key == "vram"
        vram = [p.vram_used for p in table.rows]
        assert vram == sorted(vram, reverse=True)
        await pilot.press("s")
        assert table.sort_key == "sm"
        await pilot.press("r")
        assert table.sort_reverse
        assert app.changes["sort_key"] == "sm" and app.changes["sort_reverse"] is True

        # Click the Pid: header (first row inside the border, first column).
        region = table.region
        header = (region.x + 2 + 3, region.y + 1)
        await pilot.click(offset=header)
        assert table.sort_key == "pid"
        pids = [p.pid for p in table.rows]
        assert pids == sorted(pids)
        await pilot.click(offset=header)  # same header: reverse
        assert [p.pid for p in table.rows] == sorted(pids, reverse=True)

    run(make_app(count=3), scenario)


def test_process_selection_and_scrolling():
    async def scenario(pilot, app):
        table = app.table
        await pilot.press("down")
        assert table.selected_index == 0
        await pilot.press("end")
        assert table.selected_index == len(table.rows) - 1
        assert table.scroll_top > 0  # list overflows at 80x24 with 8 GPUs
        await pilot.press("home")
        assert table.selected_index == 0 and table.scroll_top == 0

    run(make_app(count=8), scenario, size=(80, 24))


def test_click_panel_focuses_and_double_click_opens_detail():
    async def scenario(pilot, app):
        panel = app.panels[1]
        await pilot.click(panel)
        assert app.focus_pos == 1
        await pilot.double_click(panel)
        await pilot.pause(0.2)
        assert app.detail_mode and app.table.gpu_filter == 1

    run(make_app(count=2), scenario)


def test_interval_keys_clamp():
    async def scenario(pilot, app):
        app.sampler.interval = 0.5
        await pilot.press("plus")
        assert app.sampler.interval == pytest.approx(0.25)
        for _ in range(10):
            await pilot.press("plus")
        assert app.sampler.interval == pytest.approx(0.1)
        for _ in range(20):
            await pilot.press("minus")
        assert app.sampler.interval == pytest.approx(5.0)
        assert app.changes["interval_ms"] == 5000
        assert "┐- 5000ms +┌" in screen_text(app)

    run(make_app(), scenario)


def test_theme_cycle_and_help():
    async def scenario(pilot, app):
        await pilot.press("t")
        assert app.gtheme.name == "dracula"
        assert str(app.screen.styles.background.hex).lower() == "#282a36"
        for _ in range(3):
            await pilot.press("t")
        assert app.gtheme.name == "default"
        assert app.changes["theme"] == "default"

        await pilot.press("question_mark")
        await pilot.pause(0.1)
        assert isinstance(app.screen, HelpOverlay)
        assert "this help" in screen_text(app)
        await pilot.press("x")  # any key closes it, without side effects
        await pilot.pause(0.1)
        assert not isinstance(app.screen, HelpOverlay)
        assert app.is_running

    run(make_app(), scenario)


def test_quit_key():
    async def scenario(pilot, app):
        await pilot.press("q")
        await pilot.pause(0.1)
        assert not app.is_running

    run(make_app(), scenario)


def test_gpu_lost_mid_run_does_not_crash():
    async def scenario(pilot, app):
        # The fake GPU 0 disappears after 45 samples; warm-up did 40.
        await pilot.pause(1.2)
        assert app.sampler.devices[0].lost
        assert app.is_running
        text = screen_text(app)
        assert "GPU no longer available" in text
        assert "┘LOST└" in text
        assert all(p.gpu_index != 0 for p in app.table.rows)
        await pilot.press("a")  # detail view of a lost GPU
        await pilot.pause(0.2)
        assert "no longer available" in screen_text(app)

    run(make_app(count=2, lose_after={0: 45}), scenario)


@pytest.mark.parametrize("size", [(80, 24), (60, 16), (40, 10), (200, 60)])
def test_resizes_without_crashing(size):
    async def scenario(pilot, app):
        await pilot.resize_terminal(*size)
        await pilot.pause(0.3)
        await pilot.press("a")
        await pilot.pause(0.2)
        await pilot.press("a")
        await pilot.pause(0.2)
        assert app.is_running
        text = screen_text(app)
        assert "┐proc┌" in text or size[1] < 12
        assert "gpu0" in text

    run(make_app(count=4), scenario)


def test_overview_layout_math():
    # 1-2 GPUs: stacked full width, ~65 % of the height, equal box heights
    assert overview_layout(2, 80, 24) == (1, 2, 8)  # 16 rows of GPU boxes, proc box 8
    assert overview_layout(2, 160, 50) == (1, 2, 16)  # 32 rows (65 %), proc box 18
    assert overview_layout(1, 120, 40) == (1, 1, 26)
    # 3+ GPUs: two columns only from 160 columns; proc box shrinks to 8 rows
    assert overview_layout(6, 200, 60) == (2, 3, 17)
    assert overview_layout(6, 120, 40) == (1, 6, 5)
    # never below a 3-row box (the GPU area scrolls instead)
    assert overview_layout(6, 80, 24)[2] == 3
    # no process box: the GPU boxes fill the screen
    assert overview_layout(2, 80, 24, with_table=False) == (1, 2, 12)


# -- Intel / mixed-vendor UI behaviour ------------------------------------------------


def make_intel_app(tmp_path, with_clients=False, with_nvidia=False, pmu_denied=True):
    from fake_backend import ScriptedBackend
    from fake_sysfs import FakeSysfs, drm_fdinfo
    from gputop.backends.base import GPUProcess
    from gputop.backends.machine import MachineBackend
    from gputop.backends.intel import IntelBackend

    tree = FakeSysfs(tmp_path)
    tree.add_card(0, pci="0000:00:02.0")
    tree.add_coretemp()
    tree.add_pmu()
    if pmu_denied:
        tree.pmu_error = 13  # EACCES
    if with_clients:
        tree.add_proc(50, "kitty", ["kitty"], {3: ("/dev/dri/renderD128", drm_fdinfo())})
    intel = IntelBackend(root=tree.root, counter_opener=tree.open_counter, clock=tree.clock)
    if with_nvidia:
        cuda_app = GPUProcess(pid=9, gpu_index=0, vram_used=1 << 30, user="u", command="cuda-app")
        nv = ScriptedBackend(count=1, processes={0: [cuda_app]})
        backend = MachineBackend([("nvidia", lambda: nv), ("intel", lambda: intel)], use_pci=False)
    else:
        backend = intel
    sampler = Sampler(backend, interval=0.1, resolve_processes=False)
    sampler.discover()
    for _ in range(3):
        sampler.poll_once()
    return GpuTopApp(sampler, available_themes(None), "default", backend_label="test")


def test_intel_igpu_panel_hides_vram_and_shows_hint(tmp_path):
    async def scenario(pilot, app):
        text = screen_text(app)
        assert "vram" not in text  # shared memory: no fake VRAM bar
        assert "util hidden: needs" in text and "perf_event_paranoid=0" in text
        assert "temp 61°C" in text
        assert "clk" in text  # clock history stands in for the missing util graph
        # no fdinfo clients: process box hidden entirely
        assert not app.table.display
        assert "┐proc┌" not in text
        await pilot.press("a")
        await pilot.pause(0.3)
        text = screen_text(app)
        assert "util hidden" in text
        shown = {type(b).__name__ for b in app._detail.boxes if b.display}
        assert shown == {"PowerTempBox"}  # no VRAM box, no PCIe box (no data)
        assert not app.table.display

    run(make_intel_app(tmp_path), scenario)


def test_intel_with_fdinfo_shows_table_without_empty_columns(tmp_path):
    async def scenario(pilot, app):
        assert app.table.display
        text = screen_text(app)
        assert "kitty" in text
        header = next(line for line in text.splitlines() if "Pid:" in line)
        assert "Vram" not in header and "Sm%" not in header  # all-empty columns hidden
        assert "Type:" in header

    run(make_intel_app(tmp_path, with_clients=True), scenario)


def test_mixed_vendors_table_follows_focused_gpu(tmp_path):
    async def scenario(pilot, app):
        assert len(app.panels) == 2
        assert app.table.display  # the NVIDIA-like GPU has processes
        assert "cuda-app" in screen_text(app)
        await pilot.press("2", "a")  # detail view of the Intel GPU
        await pilot.pause(0.3)
        assert app.detail_mode and not app.table.display
        await pilot.press("1")  # back to the other GPU, still in detail view
        await pilot.pause(0.3)
        assert app.table.display
        await pilot.press("a")
        await pilot.pause(0.3)
        assert app.table.display and "cuda-app" in screen_text(app)

    run(make_intel_app(tmp_path, with_nvidia=True), scenario)


# -- PCI-only GPUs (off / suspended / no driver) --------------------------------------


def make_pci_app(tmp_path):
    from fake_sysfs import FakeSysfs, drm_fdinfo
    from gputop.backends.intel import IntelBackend
    from gputop.backends.machine import MachineBackend

    tree = FakeSysfs(tmp_path)
    tree.add_card(0, pci="0000:00:02.0")
    tree.add_pci("0000:01:00.0", driver="nvidia", runtime_status="suspended", power_state="D3cold")
    tree.add_pci("0000:02:00.0", driver=None, enable=0, power_state="D3cold")
    tree.add_proc(50, "kitty", ["kitty"], {3: ("/dev/dri/renderD128", drm_fdinfo())})
    m = MachineBackend([("intel", lambda: IntelBackend(root=tree.root, counter_opener=tree.open_counter,
                                                       clock=tree.clock))], root=str(tree.root))
    sampler = Sampler(m, interval=0.1, resolve_processes=False)
    sampler.discover()
    for _ in range(3):
        sampler.poll_once()
    return GpuTopApp(sampler, available_themes(None), "default", backend_label="test"), tree


def test_off_and_suspended_gpus_get_boxes(tmp_path):
    app, tree = make_pci_app(tmp_path)

    async def scenario(pilot, app):
        text = screen_text(app)
        assert len(app.panels) == 3
        assert "┐● ACTIVE┌" in text and "┐● SUSPENDED┌" in text and "┐● OFF┌" in text
        assert "suspended (D3cold) - not queried to avoid waking it" in text
        assert "GPU is off - no data" in text
        assert "┐0000:01:00.0┌" in text  # name + PCI address in the title
        assert "driver nvidia" in text and "power  D3cold" in text
        # processes only for the ACTIVE GPU
        assert {p.gpu_index for p in app.table.rows} == {0}
        await pilot.press("2")  # hotkeys cover PCI-only GPUs too
        assert app.focus_pos == 1
        await pilot.press("a")
        await pilot.pause(0.3)
        assert not app.table.display  # a suspended GPU has no processes
        assert not any(b.display for b in app._detail.boxes)  # no stale graphs

    run(app, scenario, size=(120, 40))


def test_rediscover_key_rebuilds_boxes(tmp_path):
    app, tree = make_pci_app(tmp_path)

    async def scenario(pilot, app):
        assert len(app.panels) == 3
        tree.add_pci("0000:03:00.0", driver=None)  # hot-plugged GPU
        await pilot.press("R")
        for _ in range(20):
            await pilot.pause(0.1)
            if len(app.panels) == 4:
                break
        assert len(app.panels) == 4
        assert "┐● NO DRIVER┌" in screen_text(app)

    run(app, scenario, size=(120, 50))


def test_sigusr1_requests_rediscovery(tmp_path):
    import os
    import signal

    app, tree = make_pci_app(tmp_path)

    async def scenario(pilot, app):
        signal.signal(signal.SIGUSR1, lambda s, f: app.request_rediscover())
        try:
            tree.add_pci("0000:03:00.0", driver=None)
            os.kill(os.getpid(), signal.SIGUSR1)
            for _ in range(20):
                await pilot.pause(0.1)
                if len(app.panels) == 4:
                    break
            assert len(app.panels) == 4
        finally:
            signal.signal(signal.SIGUSR1, signal.SIG_DFL)

    run(app, scenario, size=(120, 50))
