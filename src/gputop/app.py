"""The Textual application: layout, key bindings, view switching."""

from __future__ import annotations

import math
import time

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.message import Message
from textual.widgets import Input

from . import __version__
from .config import Config
from .sampler import Sampler
from .themes import Theme
from .widgets.common import LinesWidget, reset_titles, set_title
from .widgets.gpu_panel import GpuDetail, GpuPanel
from .widgets.help_overlay import HelpOverlay
from .widgets.process_table import ProcessTable

INTERVAL_STEPS_MS = (100, 200, 250, 500, 750, 1000, 1500, 2000, 3000, 5000)
PANEL_MIN_WIDTH = 38
PROC_MIN_HEIGHT = 6  # border + header + 3 rows


class SampleReady(Message):
    """Posted (thread-safely) by the sampler thread after each poll."""

    def __init__(self, generation: int) -> None:
        super().__init__()
        self.generation = generation


class FilterBar(Input):
    """One-line process filter docked at the bottom of the frame."""

    BINDINGS = [Binding("escape", "cancel", "clear filter", show=False)]

    class Cancelled(Message):
        pass

    def action_cancel(self) -> None:
        self.post_message(self.Cancelled())


class TopBar(LinesWidget):
    """The frame's top edge, drawn as its own 1-row widget.

    Keeping the ticking clock out of the frame's border title means a clock
    update repaints one row instead of the whole screen.
    """

    DEFAULT_CSS = "TopBar { height: 1; }"

    def __init__(self, theme: Theme, **kwargs) -> None:
        super().__init__(**kwargs)
        self.gputop_theme = theme
        self.left = Text()
        self.right = Text()

    def set_parts(self, left: Text, right: Text) -> None:
        if (left.plain, right.plain, left.spans, right.spans) != (
            self.left.plain, self.right.plain, self.left.spans, self.right.spans
        ):
            self.left, self.right = left, right
            self.refresh()

    def render_text(self) -> Text:
        th = self.gputop_theme
        edge = th.style("graph_text")
        w = self.content_size.width
        line = Text(no_wrap=True)
        line.append("╭─ ", edge)
        line.append_text(self.left)
        tail_len = self.right.cell_len + 4  # " " + right + " ─╮"
        gap = w - line.cell_len - tail_len - 1
        if gap >= 1:
            line.append(" " + "─" * gap + " ", edge)
            line.append_text(self.right)
            line.append(" ─╮", edge)
        else:
            line.append(" " + "─" * max(0, w - line.cell_len - 2), edge)
            line.append("╮", edge)
        line.truncate(w)
        return line


def overview_layout(n: int, width: int, height: int) -> tuple[int, int, int]:
    """(columns, rows, panel_height) for ``n`` GPU panels in ``width``x``height``.

    Panels are at least ``PANEL_MIN_WIDTH`` wide. Their height shrinks towards
    3 rows (1 content line) on small terminals and grows up to 12 on tall ones
    so graphs get taller, always leaving room for the process table.
    """
    n = max(1, n)
    cols = max(1, min(n, width // PANEL_MIN_WIDTH))
    rows = math.ceil(n / cols)
    avail = max(3 * rows, height - PROC_MIN_HEIGHT)
    ideal = max(6, min(12, (height * 55 // 100) // rows))
    ph = max(3, min(ideal, avail // rows))
    return cols, rows, ph


class GpuTopApp(App[None]):
    TITLE = "gputop"
    ENABLE_COMMAND_PALETTE = False
    AUTO_FOCUS = None  # the (hidden) filter input must not grab keys on start

    CSS = """
    Screen { layout: vertical; }
    #frame {
        height: 1fr;
        border: round white;
        border-top: none;
        border-subtitle-align: right;
        layout: vertical;
    }
    #overview {
        layout: grid;
        grid-size: 2;
        grid-gutter: 0 1;
        height: auto;
        overflow-y: auto;
    }
    #overview > GpuPanel { height: 1fr; }
    #detail-host { height: 2fr; display: none; }
    ProcessTable { height: 1fr; min-height: 4; }
    #filter {
        dock: bottom;
        display: none;
        height: 1;
        border: none;
        padding: 0 1;
    }
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
        super().__init__()
        self.sampler = sampler
        self.themes = themes
        self.theme_order = self._theme_order(themes)
        self.gtheme = themes.get(theme_name) or themes["default"]
        self.config = config or Config()
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
        self.topbar = TopBar(self.gtheme)
        self.frame = Container(id="frame")
        self.overview = Container(id="overview")
        self.detail_host = Container(id="detail-host")
        self.table = ProcessTable(
            self.gtheme, sort_key=self.config.sort_key, sort_reverse=self.config.sort_reverse
        )
        self.filter_bar = FilterBar(
            placeholder="filter by command, user or PID · Enter keep · Esc clear", id="filter"
        )
        yield self.topbar
        with self.frame:
            with self.overview:
                for pos, dev in enumerate(self.sampler.devices):
                    panel = GpuPanel(dev, self.gtheme, hotkey=pos + 1 if pos < 9 else None)
                    self.panels.append(panel)
                    yield panel
            yield self.detail_host
            yield self.table
            yield self.filter_bar

    @property
    def _live(self) -> bool:
        """False before compose finished or once the UI is being torn down."""
        table = getattr(self, "table", None)
        return table is not None and table.is_attached and not self._exit

    def on_mount(self) -> None:
        self.sampler.on_update = lambda gen: self.post_message(SampleReady(gen))
        self.apply_theme(self.gtheme)
        if self.panels:
            self.panels[0].set_selected(True)
        self.set_interval(1.0, self.update_header)
        if self.detail_mode:
            self.call_after_refresh(self._enter_detail)
        self.call_after_refresh(self.relayout)
        self.sampler.start()

    def on_unmount(self) -> None:
        self.sampler.on_update = None
        self.sampler.stop()

    # -- data updates ---------------------------------------------------------

    def on_sample_ready(self, message: SampleReady) -> None:
        if not self._live:
            return
        for panel in self.panels:
            panel.update_data()
        if self._detail is not None:
            self._detail.update_data()
        self.table.set_processes(self.sampler.all_processes())

    # -- layout ---------------------------------------------------------------

    def on_resize(self, event: events.Resize) -> None:
        self.call_after_refresh(self.relayout)

    def relayout(self) -> None:
        if not self._live:
            return
        frame = self.frame
        w, h = frame.content_size
        if w <= 0 or h <= 0:
            return
        # History must cover the widest possible graph (2 samples per cell).
        if self.sampler.history_len < w * 2:
            self.sampler.set_history_len(w * 2 + 64)
        cols, rows, ph = overview_layout(len(self.panels), w, h)
        key = (w, h, cols, rows, ph, self.detail_mode)
        if key != self._last_layout:
            self._last_layout = key
            overview = self.overview
            overview.styles.grid_size_columns = cols
            overview.set_styles(f"grid-rows: {ph};")
            overview.styles.height = min(rows * ph, max(3, h - PROC_MIN_HEIGHT))
            if self._detail is not None:
                self._detail.set_columns(3 if w >= 110 or h < 30 else 2)
        self.update_header()

    def update_header(self) -> None:
        if not self._live:
            return
        th = self.gtheme
        frame = self.frame
        right = Text()
        if self.backend_label:
            right.append(self.backend_label, th.style("graph_text"))
            right.append(" ─ ", th.style("div_line"))
        ms = round(self.sampler.interval * 1000)
        right.append(f"{ms}ms", th.style("title"))
        right.append(" ─ ", th.style("div_line"))
        right.append(time.strftime("%H:%M:%S"), th.style("title", bold=True))

        left = Text()
        left.append("gputop", th.style("title", bold=True))
        mode = "detail" if self.detail_mode else f"{len(self.panels)} GPU{'s' if len(self.panels) != 1 else ''}"
        left.append(f" {mode}", th.style("hi_fg"))

        self.topbar.set_parts(left, right)

        hints = Text()
        for i, (key, label) in enumerate(
            (("h", "help"), ("a", "view"), ("s", "sort"), ("f", "filter"), ("t", "theme"), ("q", "quit"))
        ):
            if i:
                hints.append("  ")
            hints.append(key, th.style("hi_fg", bold=True))
            hints.append(" " + label, th.style("main_fg"))
        set_title(frame, "border_subtitle", hints)

    # -- theme ----------------------------------------------------------------

    def apply_theme(self, theme: Theme) -> None:
        self.gtheme = theme
        self.screen.styles.background = theme["main_bg"]
        self.screen.styles.color = theme["main_fg"]
        frame = self.frame
        edge = theme["graph_text"]
        frame.styles.border_left = ("round", edge)
        frame.styles.border_right = ("round", edge)
        frame.styles.border_bottom = ("round", edge)
        reset_titles(frame)
        topbar = self.topbar
        topbar.gputop_theme = theme
        topbar.refresh()
        for panel in self.panels:
            panel.apply_theme(theme)
        if self._detail is not None:
            self._detail.apply_theme(theme)
        self.table.apply_theme(theme)
        flt = self.filter_bar
        flt.styles.background = theme["selected_bg"]
        flt.styles.color = theme["selected_fg"]
        self.update_header()

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
        host = self.detail_host
        overview = self.overview
        state = self.panels[self.focus_pos].state
        if self._detail is None or self._detail_pos != self.focus_pos:
            if self._detail is not None:
                self._detail.remove()
            self._detail = GpuDetail(state, self.gtheme)
            self._detail_pos = self.focus_pos
            host.mount(self._detail)
            w, h = self.frame.content_size
            self.call_after_refresh(self._detail.set_columns, 3 if w >= 110 or h < 30 else 2)
        overview.display = False
        host.display = True
        self.table.set_gpu_filter(state.index)
        self.update_header()

    def _exit_detail(self) -> None:
        if self._detail is not None:
            self._detail.remove()
            self._detail = None
            self._detail_pos = None
        self.detail_host.display = False
        self.overview.display = True
        self.table.set_gpu_filter(None)
        self.update_header()

    # -- process table --------------------------------------------------------

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
        flt = self.filter_bar
        flt.display = True
        flt.value = self.table.filter_text
        flt.focus()

    @on(Input.Changed, "#filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        self.table.set_filter(event.value)

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self, event: Input.Submitted) -> None:
        self._close_filter(keep=True)

    @on(FilterBar.Cancelled)
    def _filter_cancelled(self) -> None:
        self._close_filter(keep=False)

    def _close_filter(self, keep: bool) -> None:
        flt = self.filter_bar
        if not keep:
            flt.value = ""
            self.table.set_filter("")
        flt.display = False
        self.set_focus(None)

    # -- misc -----------------------------------------------------------------

    def action_interval(self, direction: int) -> None:
        current = round(self.sampler.interval * 1000)
        if direction < 0:
            smaller = [s for s in INTERVAL_STEPS_MS if s < current]
            new = smaller[-1] if smaller else INTERVAL_STEPS_MS[0]
        else:
            larger = [s for s in INTERVAL_STEPS_MS if s > current]
            new = larger[0] if larger else INTERVAL_STEPS_MS[-1]
        self.sampler.interval = new / 1000
        self.changes["interval_ms"] = new
        self.update_header()

    def action_help(self) -> None:
        extra = f"gputop {__version__} · backend {self.backend_label or self.sampler.backend.name}"
        self.push_screen(HelpOverlay(self.gtheme, extra))

    async def action_quit(self) -> None:
        self.exit()
