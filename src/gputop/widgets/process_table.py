"""The process list, drawn as btop's proc box.

::

    ╭─┐proc┌┐filter┌─────────────────────────┐reverse┌┐sort: vram┌─╮
    │    Pid: User:     Command:              Gpu:   Vram ↓  Sm% Type:   │
    │   4211 nik       ollama serve              0    6.2G   81 Compute │
    ╰┘↑ select ↓└┘detail ↵└┘theme t└┘help h└┘quit q└──────────┘1/12└╯

Rows fade from main_fg towards inactive_fg further down the list (btop's
``proc_gradient``); a scrollbar appears on the right when the list overflows.
Filter text is typed straight into the ``┐filter: …┌`` tab, like btop.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.message import Message

from .. import fmt
from ..backends.base import GPUProcess
from ..themes import Theme, blend
from .box import Tab, hotkey_tab
from .gpu_panel import BoxWidget


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
    Column("pid", "Pid:", 7, "right", 99, lambda p: p.pid, False),
    Column("user", "User:", 9, "left", 2, lambda p: p.user.lower(), False),
    Column("command", "Command:", 0, "left", 99, lambda p: p.command.lower(), False),
    Column("gpu", "Gpu:", 4, "right", 3, lambda p: p.gpu_index, False),
    Column("vram", "Vram", 8, "right", 99, lambda p: _num(p.vram_used), True),
    Column("sm", "Sm%", 5, "right", 4, lambda p: _num(p.sm_util), True),
    Column("type", "Type:", 8, "left", 1, lambda p: p.type, False),
)
SORT_KEYS: tuple[str, ...] = tuple(c.key for c in COLUMNS)
_BY_KEY = {c.key: c for c in COLUMNS}
MIN_COMMAND_W = 12
FADE = 0.95  # how far the last visible row fades towards inactive_fg


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


def _hint(label: str, key: str) -> Tab:
    text = f"{label} {key}"
    return Tab(text, hotkey=len(text) - 1, bold=False)


class ProcessTable(BoxWidget):
    color_key = "proc_box"

    BINDINGS = [
        Binding("escape", "filter_cancel", show=False),
        Binding("enter", "filter_accept", show=False),
        Binding("backspace", "filter_backspace", show=False),
    ]

    class SortChanged(Message):
        def __init__(self, key: str, reverse: bool) -> None:
            super().__init__()
            self.key = key
            self.reverse = reverse

    def __init__(self, theme: Theme, sort_key: str = "vram", sort_reverse: bool = False, **kwargs) -> None:
        super().__init__(theme, **kwargs)
        self.can_focus = False  # only focusable while the filter is being typed
        self.sort_key = sort_key if sort_key in SORT_KEYS else "vram"
        self.sort_reverse = sort_reverse
        self.filter_text = ""
        self.editing = False
        self.gpu_filter: int | None = None
        self.show_gpu_column = True
        self._all: list[GPUProcess] = []
        self.rows: list[GPUProcess] = []
        self.selected_pid: int | None = None
        self.scroll_top = 0
        self._layout: list[tuple[Column, int]] = []
        self._digest: tuple | None = None
        self.mem_totals: dict[int, int] = {}  # gpu index -> VRAM bytes (colours VRAM by share)

    # -- data -----------------------------------------------------------------

    def set_processes(self, procs: list[GPUProcess]) -> None:
        self._all = list(procs)
        self._rebuild()

    def _rebuild(self, force: bool = False) -> None:
        rows = filter_processes(self._all, self.filter_text, self.gpu_filter)
        self.rows = sort_processes(rows, self.sort_key, self.sort_reverse)
        if self.selected_pid is not None and self.selected_index is None:
            self.selected_pid = None  # selected process exited or was filtered
        self._clamp_scroll()
        # Repaint only if something visible changed (saves CPU when idle).
        digest = (
            self.sort_key, self.sort_reverse, self.filter_text, self.editing, self.gpu_filter,
            self.selected_pid, self.scroll_top, len(self.rows),
            tuple((p.pid, p.user, p.command, p.gpu_index, p.vram_used, p.sm_util, p.type)
                  for p in self.rows[self.scroll_top: self.scroll_top + max(1, self.visible_rows)]),
        )
        if force or digest != self._digest:
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
        return max(0, self.size.height - 3)  # two borders + header

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
        self._rebuild(force=True)

    def set_sort(self, key: str, reverse: bool | None = None) -> None:
        if key in SORT_KEYS:
            self.sort_key = key
        if reverse is not None:
            self.sort_reverse = reverse
        self._rebuild(force=True)
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
        self._rebuild(force=True)

    def select_edge(self, end: bool) -> None:
        if self.rows:
            self.selected_pid = self.rows[-1 if end else 0].pid
            self._rebuild(force=True)

    def page(self, direction: int) -> None:
        self.move_selection(direction * max(1, self.visible_rows - 1))

    def clear_selection(self) -> bool:
        if self.selected_pid is None:
            return False
        self.selected_pid = None
        self._rebuild(force=True)
        return True

    # -- inline filter editing (btop types the filter into the title) ----------

    def start_edit(self) -> None:
        self.editing = True
        self.can_focus = True
        self.focus()
        self._rebuild(force=True)

    def _stop_edit(self) -> None:
        self.editing = False
        self.can_focus = False
        self.app.set_focus(None)
        self._rebuild(force=True)

    def check_consume_key(self, key: str, character: str | None) -> bool:
        return self.editing and character is not None and character.isprintable()

    def on_key(self, event: events.Key) -> None:
        if self.editing and event.is_printable and event.character:
            event.stop()
            event.prevent_default()
            self.set_filter(self.filter_text + event.character)

    def action_filter_backspace(self) -> None:
        if self.editing:
            self.set_filter(self.filter_text[:-1])

    def action_filter_accept(self) -> None:
        self._stop_edit()

    def action_filter_cancel(self) -> None:
        self.filter_text = ""
        self._stop_edit()

    # -- chrome ---------------------------------------------------------------

    def top_tabs(self):
        left = [Tab("proc")]
        if self.editing:
            left.append(Tab(f"filter: {self.filter_text}█", hotkey=0, bold=False))
        elif self.filter_text:
            left.append(Tab(f"filter: {self.filter_text}", hotkey=0, bold=False))
        else:
            left.append(hotkey_tab("filter", bold=False))
        right = []
        if self.gpu_filter is not None:
            right.append(Tab(f"gpu{self.gpu_filter} only", bold=False))
        right.append(hotkey_tab("reverse", bold=False))
        right.append(hotkey_tab(f"sort: {self.sort_key}", "s"))
        return left, None, right

    def bottom_tabs(self):
        select = Tab("↑ select ↓", marks={0: "hi_fg", 9: "hi_fg"}, bold=False)
        left = [select, _hint("detail", "↵"), _hint("theme", "t"), _hint("help", "h"), _hint("quit", "q")]
        idx = self.selected_index
        right = [Tab(f"{0 if idx is None else idx + 1}/{len(self.rows)}")]
        return left, right

    # -- rendering ------------------------------------------------------------

    def content(self, w: int, h: int) -> list[Text]:
        th = self.gputop_theme
        hide = set() if self.show_gpu_column else {"gpu"}
        # Columns no listed process has data for (e.g. VRAM and SM% for Intel
        # iGPU clients) are hidden instead of filled with dashes.
        if self.rows and all(p.vram_used is None for p in self.rows):
            hide.add("vram")
        if self.rows and all(p.sm_util is None for p in self.rows):
            hide.add("sm")
        vis = max(0, h - 1)
        overflow = len(self.rows) > vis
        table_w = w - 2 if overflow else w  # reserve a gap + the scrollbar column
        self._layout = layout_columns(table_w, hide)
        lines = [self._header(th)]
        if not self.rows:
            msg = "no matching processes" if self.filter_text else "no GPU processes"
            lines.append(Text(msg, th.style("inactive_fg")))
            return lines

        sel = self.selected_pid
        sel_style = Style(bgcolor=th["selected_bg"], color=th["selected_fg"], bold=True)
        window = self.rows[self.scroll_top: self.scroll_top + vis]
        for i, p in enumerate(window):
            fade = FADE * i / max(1, vis - 1)
            lines.append(self._row(p, th, table_w, sel_style if p.pid == sel else None, fade))
        if overflow:
            bar = self._scrollbar(vis)
            lines[0].append(" " * (table_w - lines[0].cell_len + 2), th.base_style)
            for i in range(1, len(lines)):
                lines[i].append(" ", th.base_style)
                lines[i].append(bar[i - 1], th.style("main_fg", bold=True))
        return lines

    def _scrollbar(self, vis: int) -> list[str]:
        """btop-style: ↑ at the top, ↓ at the bottom, █ marking the position."""
        bar = [" "] * vis
        if vis >= 1:
            span = max(1, len(self.rows) - vis)
            pos = 1 + round((vis - 3) * self.scroll_top / span) if vis > 2 else 0
            bar[max(0, min(vis - 1, pos))] = "█"
            bar[0] = "↑"
            bar[-1] = "↓"
        return bar

    def _header(self, th: Theme) -> Text:
        line = Text(no_wrap=True)
        title = th.style("title", bold=True)
        arrow_style = th.style("main_fg", bold=True)  # btop: header title, arrow main_fg
        for i, (col, w) in enumerate(self._layout):
            if i:
                line.append(" ", th.base_style)
            label = col.header
            arrow = ""
            if col.key == self.sort_key:
                arrow = "↓" if col.natural_desc != self.sort_reverse else "↑"
                label = f"{col.header} {arrow}"
                if len(label) > w:
                    label = col.header.rstrip(":") + arrow
            label = label[:w]
            cell = label.rjust(w) if col.align == "right" else label.ljust(w)
            for ch in cell:
                line.append(ch, arrow_style if arrow and ch == arrow else title)
        return line

    def _row(self, p: GPUProcess, th: Theme, width: int, sel: Style | None, fade: float) -> Text:
        line = Text(no_wrap=True)
        faded = th.color_style(blend(th["main_fg"], th["inactive_fg"], fade))
        for i, (col, w) in enumerate(self._layout):
            if i:
                line.append(" ", th.base_style)
            style = faded
            if col.key == "pid":
                val = str(p.pid)
            elif col.key == "user":
                val = p.user or "?"
            elif col.key == "command":
                val = p.command or p.name_hint or f"[{p.pid}]"
            elif col.key == "gpu":
                val = str(p.gpu_index)
            elif col.key == "vram":
                val = fmt.bytes_short(p.vram_used) if p.vram_used is not None else "-"
                if p.vram_used is not None:
                    total = self.mem_totals.get(p.gpu_index) or 8 * 1024**3
                    style = th.grad("proc", min(1.0, p.vram_used / total))
            elif col.key == "sm":
                val = "-" if p.sm_util is None else f"{p.sm_util:.0f}"
                if p.sm_util is not None:
                    style = th.grad("proc", p.sm_util / 100)
            else:  # type
                val = p.type
                if p.type != "Graphics":
                    style = th.style("proc_misc")
            val = fmt.truncate(val, w)
            line.append(val.rjust(w) if col.align == "right" else val.ljust(w), style)
        if sel is not None:
            line.pad_right(max(0, width - line.cell_len))
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
        x, y = offset.x - 1 - self.pad, offset.y - 1  # inside the border and padding
        if y == 0:
            key = self.column_at(x)
            if key is None:
                return
            if key == self.sort_key:
                self.toggle_reverse()
            else:
                self.set_sort(key, False)
            return
        idx = self.scroll_top + y - 1
        if y >= 1 and 0 <= idx < len(self.rows):
            self.selected_pid = self.rows[idx].pid
            self._rebuild(force=True)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        self.move_selection(1)

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        self.move_selection(-1)

    def on_resize(self, event: events.Resize) -> None:
        self._clamp_scroll()
        self._rebuild(force=True)
