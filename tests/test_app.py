"""End-to-end UI tests: run the Textual app headless on the fake backend."""

import asyncio

import pytest

from fake_backend import make_fake
from gputop.app import FilterBar, GpuTopApp, overview_layout
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
        assert "gputop" in text
        assert "GPU 0 · RTX 4090" in text and "GPU 1 · RTX 3060" in text
        assert "util" in text and "vram" in text
        assert "PID" in text and "COMMAND" in text
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
        assert "processes · GPU 1" in screen_text(app)
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
        flt = app.query_one(FilterBar)
        assert flt.display and flt.has_focus
        # 'q', 'a' and 't' are app shortcuts; while typing they must be text.
        for ch in "qat":
            await pilot.press(ch)
        assert app.is_running and not app.detail_mode
        assert app.table.filter_text == "qat"
        assert app.table.rows == []
        assert "no matching processes" in screen_text(app)
        await pilot.press("escape")  # clears and closes, does not quit
        await pilot.pause(0.1)
        assert app.is_running
        assert app.table.filter_text == "" and not flt.display
        assert app.table.rows

        await pilot.press("f", "p", "y", "enter")
        assert app.table.filter_text == "py"
        assert not flt.display
        assert all("py" in p.command.lower() or "py" in p.user for p in app.table.rows)

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

        # Click the PID header (content row 0, first column).
        region = table.content_region
        await pilot.click(offset=(region.x + 1, region.y))
        assert table.sort_key == "pid"
        pids = [p.pid for p in table.rows]
        assert pids == sorted(pids)
        await pilot.click(offset=(region.x + 1, region.y))  # same header: reverse
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
        assert "5000ms" in screen_text(app)

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
        assert "LOST" in text
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
        assert "gputop" in screen_text(app) or size[0] < 50

    run(make_app(count=4), scenario)


def test_overview_layout_math():
    # 2 GPUs on 80x24: side by side, panels at least 3 rows tall
    cols, rows, ph = overview_layout(2, 78, 22)
    assert (cols, rows) == (2, 1) and ph >= 6
    # many GPUs on a small terminal still leave room for the process table
    cols, rows, ph = overview_layout(8, 78, 22)
    assert cols == 2 and rows == 4 and ph == 4
    assert rows * ph <= 22 - 6
    # single GPU on a huge terminal: panel grows, capped at 12
    assert overview_layout(1, 300, 100)[2] == 12
    # narrow: one column
    assert overview_layout(3, 50, 40)[0] == 1
