"""The Textual application: btop-style tiled layout, key bindings, views.

Like btop there is no outer frame and no gaps: the screen is tiled by boxes
that touch each other. Overview: one box per GPU (stacked, full width; two
columns for 3+ GPUs on wide terminals) above the process box. Detail view: a
big box for one GPU, a row of VRAM / PCIe / power+temp boxes, then the
process box.
"""

from __future__ import annotations

import math

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.message import Message

from . import __version__
from .config import Config
from .sampler import Sampler
from .themes import Theme
from .widgets.box import BoxStyle
from .widgets.gpu_panel import GpuDetail, GpuPanel
from .widgets.help_overlay import HelpOverlay
from .widgets.process_table import ProcessTable

INTERVAL_STEPS_MS = (100, 200, 250, 500, 750, 1000, 1500, 2000, 3000, 5000)
PROC_MIN_HEIGHT = 8  # process box: borders + header + 5 rows
GPU_SHARE = 0.65  # share of the height for 1-2 GPU boxes
MIN_BOX_HEIGHT = 3  # top border + 1 row + bottom border
TWO_COLUMN_WIDTH = 160


class SampleReady(Message):
    """Posted (thread-safely) by the sampler thread after each poll."""

    def __init__(self, generation: int) -> None:
        super().__init__()
        self.generation = generation


def overview_layout(n: int, width: int, height: int, with_table: bool = True) -> tuple[int, int, int]:
    """(columns, rows, box_height) for ``n`` GPU boxes on a ``width``x``height`` screen.

    1-2 GPUs: stacked, together ~65 % of the height. 3+ GPUs: two columns if
    the terminal is at least 160 wide, and the process box shrinks to its
    minimum. All GPU boxes have the same height; spare rows go to the
    process box. Without a process box the GPU boxes fill the screen.
    """
    n = max(1, n)
    cols = 2 if n >= 3 and width >= TWO_COLUMN_WIDTH else 1
    rows = math.ceil(n / cols)
    if not with_table:
        return cols, rows, max(MIN_BOX_HEIGHT, height // rows)
    target = round(height * GPU_SHARE) if n <= 2 else height - PROC_MIN_HEIGHT
    target = min(target, height - PROC_MIN_HEIGHT)
    return cols, rows, max(MIN_BOX_HEIGHT, target // rows)


class GpuTopApp(App[None]):
    TITLE = "gputop"
    ENABLE_COMMAND_PALETTE = False
    AUTO_FOCUS = None

    CSS = """
    Screen { layout: vertical; padding: 0; }
    #overview { layout: grid; grid-size: 1; grid-gutter: 0; height: auto; overflow-y: auto; }
    #detail-host { height: 1fr; display: none; }
    ProcessTable { height: 1fr; min-height: 3; }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("escape", "quit", "quit", show=False),
        Binding("tab", "focus_gpu(1)", "next GPU", priority=True, show=False),
        Binding("shift+tab", "focus_gpu(-1)", "prev GPU", priority=True, show=False),
        *[Binding(str(k), f"select_gpu({k - 1})", show=False) for k in range(1, 10)],
        Binding("a", "toggle_view", "view"),
        Binding("enter", "toggle_view", show=False),
        Binding("s", "cycle_sort", "sort"),
        Binding("r", "reverse_sort", "reverse"),
        Binding("plus,equals_sign", "interval(-1)", "faster", show=False),
        Binding("minus,underscore", "interval(1)", "slower", show=False),
        Binding("t", "cycle_theme", "theme"),
        Binding("f,slash", "filter", "filter"),
        Binding("h,question_mark", "help", "help"),
        Binding("up,k", "select_proc(-1)", show=False),
        Binding("down,j", "select_proc(1)", show=False),
        Binding("pageup", "page_proc(-1)", show=False),
        Binding("pagedown", "page_proc(1)", show=False),
        Binding("home", "edge_proc(0)", show=False),
        Binding("end", "edge_proc(1)", show=False),
    ]

    def __init__(
        self,
        sampler: Sampler,
        themes: dict[str, Theme],
        theme_name: str = "default",
        config: Config | None = None,
        backend_label: str = "",
    ) -> None:
        config = config or Config()
        # theme_background = false: let the terminal's own (default) background
        # through; Textual otherwise converts "default" to an RGB colour.
        super().__init__(ansi_color=not config.theme_background)
        self.sampler = sampler
        self.themes = themes
        self.theme_order = self._theme_order(themes)
        self.config = config
        BoxStyle.rounded = self.config.rounded_corners
        self.gtheme = (themes.get(theme_name) or themes["default"]).with_background(self.config.theme_background)
        self.backend_label = backend_label
        self.changes: dict[str, object] = {}
        self.focus_pos = 0
        self.detail_mode = self.config.view == "detail"
        self.panels: list[GpuPanel] = []
        self._detail: GpuDetail | None = None
        self._detail_pos: int | None = None
        self._last_layout: tuple | None = None

    @staticmethod
    def _theme_order(themes: dict[str, Theme]) -> list[str]:
        builtin = [n for n in ("default", "dracula", "gruvbox", "nord") if n in themes]
        return builtin + sorted(n for n in themes if n not in builtin)

    # -- composition ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        # Keep direct references: handlers run on every tick, and DOM queries
        # fail if a message arrives while the app is shutting down.
        self.overview = Container(id="overview")
        self.detail_host = Container(id="detail-host")
        self.table = ProcessTable(self.gtheme, sort_key=self.config.sort_key,
                                  sort_reverse=self.config.sort_reverse)
        with self.overview:
            for pos, dev in enumerate(self.sampler.devices):
                panel = GpuPanel(dev, self.gtheme, hotkey=pos + 1 if pos < 9 else None)
                panel.interval_ms = self._interval_ms
                self.panels.append(panel)
                yield panel
        yield self.detail_host
        yield self.table

    @property
    def _live(self) -> bool:
        """False before compose finished or once the UI is being torn down."""
        table = getattr(self, "table", None)
        return table is not None and table.is_attached and not self._exit

    def _interval_ms(self) -> int:
        return round(self.sampler.interval * 1000)

    @property
    def clock_box(self) -> GpuPanel | None:
        """The box whose top border carries the clock and interval (btop's cpu box)."""
        if self._detail is not None:
            return self._detail.main
        return self.panels[0] if self.panels else None

    def _place_clock(self) -> None:
        clock = self.clock_box
        for panel in [*self.panels, *([self._detail.main] if self._detail else [])]:
            want = panel is clock
            if panel.show_clock != want:
                panel.show_clock = want
                panel.refresh()

    def on_mount(self) -> None:
        self.sampler.on_update = lambda gen: self.post_message(SampleReady(gen))
        self.apply_theme(self.gtheme)
        if self.panels:
            self.panels[0].set_selected(True)
        self._place_clock()
        self.set_interval(1.0, self._tick_clock)
        if self.detail_mode:
            self.call_after_refresh(self._enter_detail)
        self.call_after_refresh(self._sync_table_visibility)
        self.call_after_refresh(self.relayout)
        self.sampler.start()

    def on_unmount(self) -> None:
        self.sampler.on_update = None
        self.sampler.stop()

    def _tick_clock(self) -> None:
        if self._live and self.clock_box is not None:
            self.clock_box.refresh_top()

    # -- data updates ---------------------------------------------------------

    def on_sample_ready(self, message: SampleReady) -> None:
        if not self._live:
            return
        if not self.detail_mode:
            for panel in self.panels:
                panel.update_data()
        if self._detail is not None:
            self._detail.update_data()
        self.table.mem_totals = {d.index: d.info.memory_total for d in self.sampler.devices
                                 if d.info.memory_total}
        self.table.set_processes(self.sampler.all_processes())
        self._sync_table_visibility()

    def _table_wanted(self) -> bool:
        """Show the process box only if a GPU in view has per-process data
        (hidden e.g. for Intel GPUs on kernels without DRM fdinfo)."""
        if self.detail_mode and self.panels:
            scope = [self.panels[self.focus_pos].state]
        else:
            scope = [p.state for p in self.panels]
        backend = self.sampler.backend
        for state in scope:
            try:
                if backend.supports_processes(state.info):
                    return True
            except Exception:  # lost device etc.: don't let it hide the table
                return True
        return not scope

    def _sync_table_visibility(self) -> None:
        if not self._live:
            return
        wanted = self._table_wanted()
        if self.table.display != wanted:
            self.table.display = wanted
            self._last_layout = None
            self.call_after_refresh(self.relayout)

    # -- layout ---------------------------------------------------------------

    def on_resize(self, event: events.Resize) -> None:
        self.call_after_refresh(self.relayout)

    def relayout(self) -> None:
        if not self._live:
            return
        w, h = self.screen.size
        if w <= 0 or h <= 0:
            return
        # History must cover the widest possible graph (2 samples per cell).
        if self.sampler.history_len < w * 2:
            self.sampler.set_history_len(w * 2 + 64)
        with_table = bool(self.table.display)
        cols, rows, box_h = overview_layout(len(self.panels), w, h, with_table)
        key = (w, h, cols, rows, box_h, self.detail_mode, with_table)
        if key == self._last_layout:
            return
        self._last_layout = key
        overview = self.overview
        overview.styles.grid_size_columns = cols
        if with_table:
            overview.set_styles(f"grid-rows: {box_h};")
            # Equal-height boxes; spare rows go to the process box. Too many
            # GPUs for the screen: the GPU area scrolls.
            overview.styles.height = min(rows * box_h, max(MIN_BOX_HEIGHT, h - 4))
        else:
            overview.set_styles("grid-rows: 1fr;")
            overview.styles.height = h
        if self._detail is not None:
            proc = max(PROC_MIN_HEIGHT, round(h * 0.3)) if with_table else 0
            self.detail_host.styles.height = max(MIN_BOX_HEIGHT, h - proc)

    # -- theme ----------------------------------------------------------------

    def apply_theme(self, theme: Theme) -> None:
        theme = theme.with_background(self.config.theme_background)
        self.gtheme = theme
        self.screen.styles.background = theme["main_bg"] if theme.background else "ansi_default"
        self.screen.styles.color = theme["main_fg"]
        for panel in self.panels:
            panel.apply_theme(theme)
        if self._detail is not None:
            self._detail.apply_theme(theme)
        self.table.apply_theme(theme)

    def action_cycle_theme(self) -> None:
        try:
            i = self.theme_order.index(self.gtheme.name)
        except ValueError:
            i = -1
        name = self.theme_order[(i + 1) % len(self.theme_order)]
        self.apply_theme(self.themes[name])
        self.changes["theme"] = name
        self.notify(f"theme: {name}", timeout=1.5)

    # -- GPU focus / view -----------------------------------------------------

    def _set_focus_pos(self, pos: int) -> None:
        if not self.panels:
            return
        pos %= len(self.panels)
        self.focus_pos = pos
        for i, panel in enumerate(self.panels):
            panel.set_selected(i == pos)
        if self.detail_mode:
            self._enter_detail()

    def action_focus_gpu(self, delta: int) -> None:
        self._set_focus_pos(self.focus_pos + delta)

    def action_select_gpu(self, pos: int) -> None:
        if 0 <= pos < len(self.panels):
            self._set_focus_pos(pos)

    @on(GpuPanel.Selected)
    def _panel_clicked(self, message: GpuPanel.Selected) -> None:
        for pos, panel in enumerate(self.panels):
            if panel.state.index == message.index:
                self._set_focus_pos(pos)
                if message.chain >= 2 and not self.detail_mode:
                    self.action_toggle_view()
                break

    def action_toggle_view(self) -> None:
        if not self.panels:
            return
        self.detail_mode = not self.detail_mode
        if self.detail_mode:
            self._enter_detail()
        else:
            self._exit_detail()
        self.changes["view"] = "detail" if self.detail_mode else "all"
        self._last_layout = None
        self.call_after_refresh(self.relayout)

    def _enter_detail(self) -> None:
        if not self._live:
            return
        state = self.panels[self.focus_pos].state
        if self._detail is None or self._detail_pos != self.focus_pos:
            if self._detail is not None:
                self._detail.remove()
            self._detail = GpuDetail(state, self.gtheme, hotkey=self.focus_pos + 1)
            self._detail.main.interval_ms = self._interval_ms
            self._detail.main.selected = True
            self._detail_pos = self.focus_pos
            self.detail_host.mount(self._detail)
        self.overview.display = False
        self.detail_host.display = True
        self.table.set_gpu_filter(state.index)
        self._place_clock()
        self._sync_table_visibility()
        self._last_layout = None
        self.call_after_refresh(self.relayout)

    def _exit_detail(self) -> None:
        if self._detail is not None:
            self._detail.remove()
            self._detail = None
            self._detail_pos = None
        self.detail_host.display = False
        self.overview.display = True
        self.table.set_gpu_filter(None)
        self._place_clock()
        for panel in self.panels:
            panel.update_data()
        self._sync_table_visibility()

    # -- process box ----------------------------------------------------------

    def action_cycle_sort(self) -> None:
        self.table.cycle_sort()

    def action_reverse_sort(self) -> None:
        self.table.toggle_reverse()

    @on(ProcessTable.SortChanged)
    def _sort_changed(self, message: ProcessTable.SortChanged) -> None:
        self.changes["sort_key"] = message.key
        self.changes["sort_reverse"] = message.reverse

    def action_select_proc(self, delta: int) -> None:
        self.table.move_selection(delta)

    def action_page_proc(self, direction: int) -> None:
        self.table.page(direction)

    def action_edge_proc(self, end: int) -> None:
        self.table.select_edge(bool(end))

    def action_filter(self) -> None:
        if self.table.display:
            self.table.start_edit()

    # -- misc -----------------------------------------------------------------

    def action_interval(self, direction: int) -> None:
        current = self._interval_ms()
        if direction < 0:
            smaller = [s for s in INTERVAL_STEPS_MS if s < current]
            new = smaller[-1] if smaller else INTERVAL_STEPS_MS[0]
        else:
            larger = [s for s in INTERVAL_STEPS_MS if s > current]
            new = larger[0] if larger else INTERVAL_STEPS_MS[-1]
        self.sampler.interval = new / 1000
        self.changes["interval_ms"] = new
        if self.clock_box is not None:
            self.clock_box.refresh_top()

    def action_help(self) -> None:
        extra = f"gputop {__version__} · backend {self.backend_label or self.sampler.backend.name}"
        self.push_screen(HelpOverlay(self.gtheme, extra))

    async def action_quit(self) -> None:
        self.exit()
