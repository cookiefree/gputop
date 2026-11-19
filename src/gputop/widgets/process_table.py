"""Sortable, filterable, scrollable process list (custom-rendered, btop look)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rich.style import Style
from rich.text import Text
from textual import events
from textual.message import Message
from textual.widget import Widget

from .. import fmt
from ..backends.base import GPUProcess
from ..themes import Theme
from .common import LinesWidget, join_lines, reset_titles, set_border, set_title


@dataclass(frozen=True)
class Column:
    key: str
    header: str
    width: int  # 0 = flexible
    align: str  # "left" / "right"
    drop_order: int  # lower drops first when the table is narrow; 99 = never
    sort_key: Callable[[GPUProcess], Any]
    natural_desc: bool  # natural sort direction is descending


def _num(v: float | None) -> float:
    return float("-inf") if v is None else float(v)


COLUMNS: tuple[Column, ...] = (
    Column("pid", "PID", 7, "left", 99, lambda p: p.pid, False),
    Column("user", "USER", 9, "left", 2, lambda p: p.user.lower(), False),
    Column("command", "COMMAND", 0, "left", 99, lambda p: p.command.lower(), False),
    Column("gpu", "GPU", 3, "right", 3, lambda p: p.gpu_index, False),
    Column("vram", "VRAM", 8, "right", 99, lambda p: _num(p.vram_used), True),
    Column("sm", "SM%", 4, "right", 4, lambda p: _num(p.sm_util), True),
    Column("type", "TYPE", 8, "left", 1, lambda p: p.type, False),
)
SORT_KEYS: tuple[str, ...] = tuple(c.key for c in COLUMNS)
_BY_KEY = {c.key: c for c in COLUMNS}
MIN_COMMAND_W = 12


def sort_processes(procs: list[GPUProcess], key: str, reverse: bool) -> list[GPUProcess]:
    """Sort by column ``key``; ``reverse`` flips the column's natural order
    (descending for VRAM/SM%, ascending otherwise). Ties break by PID."""
    col = _BY_KEY.get(key, _BY_KEY["vram"])
    desc = col.natural_desc != reverse
    out = sorted(procs, key=lambda p: p.pid)
    out.sort(key=col.sort_key, reverse=desc)
    return out


def filter_processes(procs: list[GPUProcess], text: str = "", gpu: int | None = None) -> list[GPUProcess]:
    needle = text.strip().lower()
    out = []
    for p in procs:
        if gpu is not None and p.gpu_index != gpu:
            continue
        if needle and needle not in p.command.lower() and needle not in p.user.lower() \
                and needle not in str(p.pid):
            continue
        out.append(p)
    return out


def layout_columns(width: int, hide: set[str] | None = None) -> list[tuple[Column, int]]:
    """Choose visible columns and widths for ``width`` cells."""
    cols = [c for c in COLUMNS if not hide or c.key not in hide]

    def total_fixed(cs):
        return sum(c.width for c in cs if c.width) + (len(cs) - 1)

    while width - total_fixed(cols) < MIN_COMMAND_W:
        droppable = [c for c in cols if c.drop_order < 99]
        if not droppable:
            break
        cols.remove(min(droppable, key=lambda c: c.drop_order))
    flex = max(1, width - total_fixed(cols))
    return [(c, c.width or flex) for c in cols]


class ProcessTable(LinesWidget, can_focus=False):
    DEFAULT_CSS = """
    ProcessTable {
        border: round white;
        border-title-align: left;
        border-subtitle-align: right;
        padding: 0 1;
        height: 1fr;
        min-height: 4;
    }
    """

    class SortChanged(Message):
        def __init__(self, key: str, reverse: bool) -> None:
            super().__init__()
            self.key = key
            self.reverse = reverse

    def __init__(self, theme: Theme, sort_key: str = "vram", sort_reverse: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.gputop_theme = theme
        self.sort_key = sort_key if sort_key in SORT_KEYS else "vram"
        self.sort_reverse = sort_reverse
        self.filter_text = ""
        self.gpu_filter: int | None = None
        self.show_gpu_column = True
        self._all: list[GPUProcess] = []
        self.rows: list[GPUProcess] = []
        self.selected_pid: int | None = None
        self.scroll_top = 0
        self._layout: list[tuple[Column, int]] = []

    # -- data -----------------------------------------------------------------

    def set_processes(self, procs: list[GPUProcess]) -> None:
        self._all = list(procs)
        self._rebuild()

    def _rebuild(self) -> None:
        rows = filter_processes(self._all, self.filter_text, self.gpu_filter)
        self.rows = sort_processes(rows, self.sort_key, self.sort_reverse)
        if self.selected_pid is not None and self.selected_index is None:
            self.selected_pid = None  # selected process exited or was filtered
        self._clamp_scroll()
        self.update_chrome()
        # Repaint only if something visible changed (saves CPU when idle).
        digest = (
            self.sort_key, self.sort_reverse, self.filter_text, self.gpu_filter,
            self.selected_pid, self.scroll_top,
            tuple((p.pid, p.user, p.command, p.gpu_index, p.vram_used, p.sm_util, p.type)
                  for p in self.rows[self.scroll_top: self.scroll_top + max(1, self.visible_rows)]),
        )
        if digest != getattr(self, "_digest", None):
            self._digest = digest
            self.refresh()

    @property
    def selected_index(self) -> int | None:
        if self.selected_pid is None:
            return None
        for i, p in enumerate(self.rows):
            if p.pid == self.selected_pid:
                return i
        return None

    @property
    def visible_rows(self) -> int:
        return max(0, self.content_size.height - 1)

    def _clamp_scroll(self) -> None:
        vis = max(1, self.visible_rows)
        idx = self.selected_index
        if idx is not None:
            if idx < self.scroll_top:
                self.scroll_top = idx
            elif idx >= self.scroll_top + vis:
                self.scroll_top = idx - vis + 1
        self.scroll_top = max(0, min(self.scroll_top, max(0, len(self.rows) - vis)))

    # -- actions (called by the app) -----------------------------------------

    def set_filter(self, text: str) -> None:
        self.filter_text = text
        self._rebuild()

    def set_gpu_filter(self, gpu: int | None) -> None:
        self.gpu_filter = gpu
        self.show_gpu_column = gpu is None
        self._rebuild()

    def set_sort(self, key: str, reverse: bool | None = None) -> None:
        if key in SORT_KEYS:
            self.sort_key = key
        if reverse is not None:
            self.sort_reverse = reverse
        self._rebuild()
        self.post_message(self.SortChanged(self.sort_key, self.sort_reverse))

    def cycle_sort(self) -> None:
        i = SORT_KEYS.index(self.sort_key)
        self.set_sort(SORT_KEYS[(i + 1) % len(SORT_KEYS)])

    def toggle_reverse(self) -> None:
        self.set_sort(self.sort_key, not self.sort_reverse)

    def move_selection(self, delta: int) -> None:
        if not self.rows:
            self.selected_pid = None
            return
        idx = self.selected_index
        if idx is None:
            idx = 0 if delta >= 0 else len(self.rows) - 1
        else:
            idx = max(0, min(len(self.rows) - 1, idx + delta))
        self.selected_pid = self.rows[idx].pid
        self._clamp_scroll()
        self.update_chrome()
        self.refresh()

    def select_edge(self, end: bool) -> None:
        if self.rows:
            self.selected_pid = self.rows[-1 if end else 0].pid
            self._clamp_scroll()
            self.update_chrome()
            self.refresh()

    def page(self, direction: int) -> None:
        self.move_selection(direction * max(1, self.visible_rows - 1))

    def clear_selection(self) -> bool:
        if self.selected_pid is None:
            return False
        self.selected_pid = None
        self.refresh()
        return True

    # -- theme / chrome -------------------------------------------------------

    def apply_theme(self, theme: Theme) -> None:
        self.gputop_theme = theme
        reset_titles(self)
        self.update_chrome()
        self.refresh()

    def update_chrome(self) -> None:
        th = self.gputop_theme
        set_border(self, th["proc_box"])
        title = Text()
        title.append("processes", th.style("title", bold=True))
        if self.gpu_filter is not None:
            title.append(f" · GPU {self.gpu_filter}", th.style("hi_fg"))
        set_title(self, "border_title", title)

        sub = Text()
        vis = self.visible_rows
        if vis and len(self.rows) > vis:
            end = min(len(self.rows), self.scroll_top + vis)
            sub.append(f"{self.scroll_top + 1}-{end}", th.style("title"))
            sub.append(f"/{len(self.rows)}", th.style("main_fg"))
        else:
            sub.append(f"{len(self.rows)}", th.style("title"))
            sub.append(" procs", th.style("main_fg"))
        if self.filter_text:
            total = len(filter_processes(self._all, "", self.gpu_filter))
            sub.append(" · filter ", th.style("main_fg"))
            sub.append(self.filter_text, th.style("hi_fg", bold=True))
            sub.append(f" ({len(self.rows)}/{total})", th.style("graph_text"))
        set_title(self, "border_subtitle", sub)

    # -- rendering ------------------------------------------------------------

    def render_text(self) -> Text:
        w, h = self.content_size
        th = self.gputop_theme
        if w <= 0 or h <= 0:
            return Text("")
        hide = set() if self.show_gpu_column else {"gpu"}
        self._layout = layout_columns(w, hide)
        lines = [self._header(th)]
        if not self.rows:
            msg = "no matching processes" if self.filter_text else "no GPU processes"
            lines.append(Text(msg, th.style("inactive_fg")))
            return join_lines(lines, w)

        sel = self.selected_pid
        sel_style = Style(bgcolor=th["selected_bg"], color=th["selected_fg"], bold=True)
        for p in self.rows[self.scroll_top: self.scroll_top + h - 1]:
            lines.append(self._row(p, th, w, sel_style if p.pid == sel else None))
        return join_lines(lines, w)

    def _header(self, th: Theme) -> Text:
        line = Text(no_wrap=True, overflow="crop")
        for i, (col, width) in enumerate(self._layout):
            if i:
                line.append(" ")
            label = col.header
            style = th.style("title", bold=True)
            if col.key == self.sort_key:
                desc = col.natural_desc != self.sort_reverse
                label = label + ("▼" if desc else "▲")
                style = th.style("hi_fg", bold=True)
            cell = label.rjust(width) if col.align == "right" else label.ljust(width)
            line.append(cell[:width], style)
        return line

    def _row(self, p: GPUProcess, th: Theme, width: int, sel: Style | None) -> Text:
        line = Text(no_wrap=True, overflow="crop")
        for i, (col, w) in enumerate(self._layout):
            if i:
                line.append(" ")
            style = th.style("main_fg")
            if col.key == "pid":
                val = str(p.pid)
            elif col.key == "user":
                val = p.user or "?"
            elif col.key == "command":
                val = p.command or p.name_hint or f"[{p.pid}]"
                style = th.style("title")
            elif col.key == "gpu":
                val = str(p.gpu_index)
            elif col.key == "vram":
                val = fmt.bytes_short(p.vram_used) if p.vram_used is not None else "-"
            elif col.key == "sm":
                val = "-" if p.sm_util is None else f"{p.sm_util:.0f}"
                if p.sm_util is not None:
                    style = th.grad("proc", p.sm_util / 100)
            else:  # type
                val = p.type
                style = th.style("proc_misc" if p.type != "Graphics" else "graph_text")
            val = fmt.truncate(val, w)
            cell = val.rjust(w) if col.align == "right" else val.ljust(w)
            line.append(cell, style)
        if sel is not None:
            line.pad_right(max(0, width - len(line.plain)))
            line.stylize(sel)
        return line

    # -- mouse ----------------------------------------------------------------

    def column_at(self, x: int) -> str | None:
        pos = 0
        for i, (col, w) in enumerate(self._layout):
            start = pos + (1 if i else 0)
            if start <= x < start + w + 1:
                return col.key
            pos = start + w
        return None

    def on_click(self, event: events.Click) -> None:
        offset = event.get_content_offset(self)
        if offset is None:
            return
        event.stop()
        if offset.y == 0:
            key = self.column_at(offset.x)
            if key is None:
                return
            if key == self.sort_key:
                self.toggle_reverse()
            else:
                self.set_sort(key, False)
            return
        idx = self.scroll_top + offset.y - 1
        if 0 <= idx < len(self.rows):
            self.selected_pid = self.rows[idx].pid
            self.refresh()

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        self.move_selection(1)

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        self.move_selection(-1)

    def on_resize(self, event: events.Resize) -> None:
        self._clamp_scroll()
        self.update_chrome()

    def on_mount(self) -> None:
        self.update_chrome()
