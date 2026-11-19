"""GPU boxes drawn like btop's cpu / mem / net boxes.

Overview box (one per GPU, btop's cpu box)::

    ╭─┐¹gpu0┌┐RTX 4090┌──┐help┌────────┐13:18:07┌──────────┐- 500ms +┌─╮
    │ util                                ╭────────────────────────────────────╮ │
    │ ⣀⣀⣀⣠⣴⣶⣷⣿⣿⣿⣿⣿⣿⣿⣶⣦⣄⣀              │vram  ■■■■■■■■■■■■■■■■■■■■ 22.7/24.0G│ │
    │ power                               │util  ■■■■■■■■■■■■■■■■■■■■       93%│ │
    │ ⣀⣀⣀⣠⣤⣶⣿⣿⣿⣿⣿⣿⣿⣶⣤⣀⣀                │...                                 │ │
    ╰┘nvidia 555.42.02└──────────────────────────────────────┘PWR└┘7 procs└╯

Left: braille area graphs (util ~60 %, power ~40 %) with a graph_text label in
the top-left corner. Right: a fixed 38-column stats panel in a div_line inner
box (a plain divider line when there is no room for the box).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence


from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.events import Click
from textual.geometry import Region
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget

from .. import fmt
from ..backends.base import THROTTLE_LABELS, THROTTLE_WARNING, GPUSample
from ..sampler import DeviceState
from ..themes import Theme
from .box import V, BoxStyle, Tab, box_bottom, box_lines, box_top, hotkey_tab, interval_tab
from .braille_graph import graph_text
from .common import LinesWidget, join_lines, text_to_segments

STATS_W = 38  # stats panel content width
LABEL_W = 6
VALUE_W = 10
MIN_GRAPH_W = 12

_EMPTY = GPUSample(index=-1)

# Status tag colours, as theme keys. In btop's Default theme these are exactly
# ACTIVE #0de756, SUSPENDED #ffd77a, OFF / NO DRIVER #b54040, UNSUPPORTED #606060.
STATUS_KEYS = {
    "ACTIVE": "proc_misc",
    "SUSPENDED": "available_mid",
    "OFF": "hi_fg",
    "NO DRIVER": "hi_fg",
    "UNSUPPORTED": "graph_text",
}


def status_tab(status: str | None) -> Tab | None:
    if not status:
        return None
    return Tab(f"● {status}", key=STATUS_KEYS.get(status, "graph_text"))


def is_live(st: DeviceState) -> bool:
    return st.latest is None or st.latest.live


def throttle_tags(sample: GPUSample | None) -> tuple[list[str], bool]:
    """(short labels, any_warning) for a sample's throttle reasons."""
    if sample is None or not sample.throttle_reasons:
        return [], False
    reasons = sample.throttle_reasons
    warn = [THROTTLE_LABELS.get(r, r.upper()) for r in reasons if r in THROTTLE_WARNING]
    if warn:
        return warn, True
    return [THROTTLE_LABELS.get(r, r.upper()) for r in reasons], False


def overlay(row: Text, x: int, label: Text) -> Text:
    """``row`` with ``label`` written over it starting at cell ``x``."""
    if x >= row.cell_len or label.cell_len == 0:
        return row
    end = min(row.cell_len, x + label.cell_len)
    out = row[:x]
    lab = label.copy()
    lab.truncate(end - x, overflow="crop")
    out.append_text(lab)
    out.append_text(row[end:])
    return out


def wrap_words(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        lines.append(cur)
    return [fmt.truncate(line, width) for line in lines]


# -- base box widget -------------------------------------------------------------


class BoxWidget(LinesWidget):
    """A widget that draws its own btop box chrome around its content."""

    color_key = "cpu_box"
    pad = 1

    def __init__(self, theme: Theme, **kwargs) -> None:
        super().__init__(**kwargs)
        self.gputop_theme = theme

    # Subclasses override these three.
    def top_tabs(self) -> tuple[list[Tab], Tab | None, list[Tab]]:
        return [], None, []

    def bottom_tabs(self) -> tuple[list[Tab], list[Tab]]:
        return [], []

    def content(self, w: int, h: int) -> list[Text]:
        return []

    @property
    def inner_size(self) -> tuple[int, int]:
        w, h = self.size
        return max(0, w - 2 - 2 * self.pad), max(0, h - 2)

    def _top(self) -> Text:
        left, center, right = self.top_tabs()
        return box_top(self.size.width, self.gputop_theme, self.color_key, left, center, right)

    def render_line_texts(self) -> list[Text]:
        w, h = self.size
        th = self.gputop_theme
        if w <= 0 or h <= 0:
            return []
        cw, ch = self.inner_size
        bl, br = self.bottom_tabs()
        return box_lines(w, h, th, self.color_key, self.content(cw, ch) if cw and ch else [],
                         top=self._top(), bottom=box_bottom(w, th, self.color_key, bl, br), pad=self.pad)

    def render_text(self) -> Text:
        return join_lines(self.render_line_texts(), self.size.width)

    def refresh_top(self) -> None:
        """Repaint only the top border (for the ticking clock)."""
        if self._strips is None or not self._strips:
            self.refresh()
            return
        w = self.size.width
        base = self.gputop_theme.base_style
        self._strips[0] = Strip(text_to_segments(self._top(), base)).crop_extend(0, w, base)
        Widget.refresh(self, Region(0, 0, w, 1))

    def apply_theme(self, theme: Theme) -> None:
        self.gputop_theme = theme
        self.refresh()

    def update_data(self) -> None:
        self.refresh()


# -- GPU box -----------------------------------------------------------------------


class GpuPanel(BoxWidget):
    """One GPU (btop's cpu box): graphs on the left, stats panel on the right."""

    color_key = "cpu_box"

    class Selected(Message):
        def __init__(self, index: int, chain: int) -> None:
            super().__init__()
            self.index = index
            self.chain = chain

    def __init__(self, state: DeviceState, theme: Theme, hotkey: int | None = None, detail: bool = False,
                 **kwargs) -> None:
        super().__init__(theme, **kwargs)
        self.state = state
        self.hotkey = hotkey
        self.selected = False
        self.detail = detail
        self.show_clock = False
        self.interval_ms: Callable[[], int] = lambda: 0

    def set_selected(self, selected: bool) -> None:
        if selected != self.selected:
            self.selected = selected
            self.refresh()

    def on_click(self, event: Click) -> None:
        event.stop()
        self.post_message(self.Selected(self.state.index, getattr(event, "chain", 1)))

    # -- chrome ---------------------------------------------------------------

    def top_tabs(self):
        st = self.state
        sup = fmt.superscript(self.hotkey) if self.hotkey is not None and self.hotkey <= 9 else ""
        left = [Tab(f"gpu{st.index}", prefix=sup, selected=self.selected),
                Tab(fmt.short_gpu_name(st.info.name))]
        if not is_live(st) and st.info.pci_address:
            left.append(Tab(st.info.pci_address, key="graph_text", bold=False))
        center = None
        right: list[Tab] = []
        tag = status_tab(st.latest.status if st.latest is not None else None)
        if tag is not None and not st.lost:
            right.append(tag)
        if self.show_clock:
            left.append(hotkey_tab("help", gap=2))
            center = Tab(time.strftime("%H:%M:%S"))
            right.append(interval_tab(self.interval_ms()))
        return left, center, right

    def bottom_tabs(self):
        st, info = self.state, self.state.info
        drv = info.driver_version or ""
        if drv[:1].isdigit() and info.vendor:
            drv = f"{info.vendor.lower()} {drv}"
        left = [Tab(drv, key="graph_text", bold=False)] if drv else []
        notes = st.latest.notes if st.latest is not None and st.latest.live else ()
        if notes:
            # Hints like "util hidden: needs kernel.perf_event_paranoid=0" live
            # in the bottom border, never over a graph. First, so they win over
            # the driver tag when space is short (long tabs get an ellipsis).
            left.insert(0, Tab(f"⚠ {notes[0]}", key="graph_text", bold=False, marks={0: "hi_fg"}))
        right: list[Tab] = []
        if not is_live(st):
            return left, right
        if st.lost:
            right.append(Tab("LOST", key="hi_fg"))
        elif st.error:
            right.append(Tab("error", key="hi_fg"))
        else:
            tags, warn = throttle_tags(st.latest)
            if warn:
                right += [Tab(t, key="hi_fg") for t in tags]
            elif tags:
                right.append(Tab(" ".join(tags).lower(), key="inactive_fg", bold=False))
            n = len(st.processes)
            right.append(Tab(f"{n} proc{'s' if n != 1 else ''}", bold=False))
        return left, right

    # -- content --------------------------------------------------------------

    def content(self, w: int, h: int) -> list[Text]:
        th, st = self.gputop_theme, self.state
        if st.lost:
            msg = Text("GPU no longer available", th.style("hi_fg", bold=True))
            return [Text("")] * max(0, (h - 1) // 2) + [msg]
        s = st.latest
        if s is None:
            return [Text("waiting for data…", th.style("inactive_fg"))]
        if not s.live:
            return offline_content(st, th, w, h)

        rows = stats_rows(st, th, STATS_W)
        if w < STATS_W + 2 + MIN_GRAPH_W:
            # Too narrow for graphs: stats only, as wide as the box allows.
            return [r for _, r in stats_rows(st, th, w)][:h]

        boxed = h >= len(rows) + 2
        panel_w = STATS_W + 2 if boxed else STATS_W + 1
        gw = w - panel_w - 1
        graph = graph_area(st, th, gw, h, util_only=self.detail)
        panel = stats_panel(rows, th, h, boxed)
        sep = Text(" ", th.base_style)
        return [g + sep + p for g, p in zip(graph, panel)]


def offline_content(st: DeviceState, th: Theme, w: int, h: int) -> list[Text]:
    """A GPU without live stats (off, suspended, no driver, unsupported driver):
    empty graph and meters in inactive_fg, a centred explanation, static info."""
    s, info = st.latest or _EMPTY, st.info
    inactive, label, value = th.style("inactive_fg"), th.style("main_fg"), th.style("title")
    meter_w = max(1, STATS_W - LABEL_W - 1 - VALUE_W)

    def empty_meter(name: str) -> Text:
        t = Text(name.ljust(LABEL_W), label)
        t.append("■" * meter_w, inactive)
        t.append(" " + "-".rjust(VALUE_W), inactive)
        return t

    def kv(k: str, v: str) -> Text:
        t = Text(k.ljust(LABEL_W + 1), label)
        t.append(v, value)
        return t

    power = s.power_state or "?"
    if s.runtime_status and s.runtime_status not in ("active",):
        power += f" · {s.runtime_status}"
    rows = [
        ("util", empty_meter("util")),
        ("vendor", kv("vendor", f"{info.vendor or '?'}  {info.pci_ids}".rstrip())),
        ("pci", kv("pci", info.pci_address or "-")),
        ("driver", kv("driver", s.driver or "none")),
        ("power", kv("power", power)),
        ("boot", kv("boot", "boot VGA" if info.boot_vga else "no")),
    ]
    rows = [(n, _fit(t, STATS_W, th)) for n, t in rows][:h]
    message = s.status_detail or (s.status or "no data").lower()

    if w < STATS_W + 2 + MIN_GRAPH_W:  # narrow: info only
        lines = [_fit(Text(message, th.style("graph_text")), w, th)] + [_fit(r, w, th) for _, r in rows]
        return lines[:h]
    boxed = h >= len(rows) + 2
    gw = w - (STATS_W + 2 if boxed else STATS_W + 1) - 1
    graph = [Text(" " * gw, th.base_style) for _ in range(h)]
    if h >= 2:
        graph[-1] = Text("⣀" * gw, inactive)  # an empty baseline, like an idle graph
    msg_lines = wrap_words(message, gw)[: max(1, h - 1)]
    y0 = max(0, (h - 1 - len(msg_lines)) // 2)  # centred above the baseline
    for i, line in enumerate(msg_lines):
        graph[y0 + i] = Text(line.center(gw), th.style("graph_text"))
    if y0 > 0:
        graph[0] = overlay(graph[0], 0, Text("util", inactive))
    panel = stats_panel(rows, th, h, boxed)
    sep = Text(" ", th.base_style)
    return [g + sep + p for g, p in zip(graph, panel)]


def stats_rows(st: DeviceState, th: Theme, width: int) -> list[tuple[str, Text]]:
    """The right-hand stats rows, in display order, each ``width`` cells."""
    s = st.latest or _EMPTY
    info = st.info
    label, value, dim = th.style("main_fg"), th.style("title"), th.style("graph_text")
    meter_w = max(1, width - LABEL_W - 1 - VALUE_W)
    out: list[tuple[str, Text]] = []

    def meter_row(name: str, frac: float | None, grad: str, val: str, val_style: Style) -> Text:
        t = Text(no_wrap=True)
        t.append(name.ljust(LABEL_W), label)
        if frac is not None:
            t.append_text(btop_meter(frac, meter_w, th, grad))
        else:
            t.append(" " * meter_w, th.base_style)
        t.append(" " + val.rjust(VALUE_W), val_style)
        return t

    def pairs(items: Sequence[tuple[str, str, Style]]) -> Text:
        t = Text(no_wrap=True)
        for i, (name, val, style) in enumerate(items):
            if i:
                t.append("  ", th.base_style)
            t.append(name, label)
            t.append(val, style)
        return t

    if s.mem_total is not None:
        frac = None if s.mem_percent is None else s.mem_percent / 100
        out.append(("vram", meter_row("vram", frac, "vram", fmt.mem_compact(s.mem_used, s.mem_total), value)))
    if s.util_gpu is not None:
        out.append(("util", meter_row("util", s.util_gpu / 100, "util", fmt.pct(s.util_gpu),
                                      th.grad("util", s.util_gpu / 100))))
    if s.power_draw is not None:
        limit = s.power_limit or info.power_limit_max
        frac = s.power_draw / limit if limit else None
        out.append(("power", meter_row("power", frac, "power", fmt.power_pair(s.power_draw, s.power_limit),
                                       value)))
    temps: list[tuple[str, str, Style]] = []
    if s.temp_core is not None:
        temps.append(("temp ", fmt.temp(s.temp_core), th.temp(s.temp_core)))
    if s.temp_mem is not None:
        temps.append(("mem ", fmt.temp(s.temp_mem), th.temp(s.temp_mem)))
    if s.fan is not None:
        temps.append(("fan ", fmt.pct(s.fan), th.grad("temp", s.fan / 100)))
    if temps:
        out.append(("temp", pairs(temps)))
    if s.clock_graphics is not None or s.clock_mem is not None:
        items = []
        if s.clock_graphics is not None:
            items.append(("clk  ", f"{s.clock_graphics} MHz", value))
        if s.clock_mem is not None:
            items.append(("mem ", f"{s.clock_mem} MHz", value))
        out.append(("clk", pairs(items)))
    codec: list[tuple[str, str, Style]] = []
    if s.util_mem is not None:
        codec.append(("mctl ", fmt.pct(s.util_mem), th.grad("util", s.util_mem / 100)))
    if s.enc_util is not None:
        codec.append(("enc ", fmt.pct(s.enc_util), th.grad("util", s.enc_util / 100)))
    if s.dec_util is not None:
        codec.append(("dec ", fmt.pct(s.dec_util), th.grad("util", s.dec_util / 100)))
    for name, v in (s.engine_util or {}).items():
        codec.append((name + " ", fmt.pct(v), th.grad("util", v / 100)))
    if s.pcie_rx is not None or s.pcie_tx is not None:
        t = Text(no_wrap=True)
        t.append("pcie".ljust(LABEL_W), label)
        t.append("▼", label)
        t.append(fmt.rate_compact(s.pcie_rx), value)
        t.append("  ▲", label)
        t.append(fmt.rate_compact(s.pcie_tx), value)
        if s.pcie_gen is not None and s.pcie_width is not None:
            link = f"  Gen{s.pcie_gen}x{s.pcie_width}"
            if t.cell_len + len(link) <= width:
                t.append(link, dim)
        out.append(("pcie", t))
    if codec:
        out.append(("mctl", pairs(codec)))
    return [(name, _fit(t, width, th)) for name, t in out]


def _fit(t: Text, width: int, th: Theme) -> Text:
    if t.cell_len > width:
        t = t.copy()
        t.truncate(width, overflow="crop")
    elif t.cell_len < width:
        t = t.copy()
        t.append(" " * (width - t.cell_len), th.base_style)
    return t


def btop_meter(frac: float, width: int, th: Theme, gradient: str) -> Text:
    """btop meter: ``■`` cells, each filled one coloured by its position along
    the bar (green at the left end, red at the right end), the rest meter_bg."""
    filled = int(round(max(0.0, min(1.0, frac)) * width))
    steps = th.gradient(gradient)
    last = len(steps) - 1
    denom = max(1, width - 1)
    t = Text(no_wrap=True)
    for i in range(filled):
        t.append("■", steps[int(round(i / denom * last))])
    if filled < width:
        t.append("■" * (width - filled), th.style("meter_bg"))
    return t


def stats_panel(rows: list[tuple[str, Text]], th: Theme, h: int, boxed: bool) -> list[Text]:
    """The stats rows framed by a div_line inner box (vertically centred), or
    behind a single div_line divider when there is no room for the box."""
    div = th.style("div_line")
    blank = Text(" " * STATS_W, th.base_style)
    if not boxed:
        lines = []
        for i in range(h):
            t = Text(V, div)
            t.append_text(rows[i][1] if i < len(rows) else blank)
            lines.append(t)
        return lines
    tl, tr, bl, br = BoxStyle.corners()
    box = [Text(tl + "─" * STATS_W + tr, div)]
    for _, r in rows:
        t = Text(V, div)
        t.append_text(r)
        t.append(V, div)
        box.append(t)
    box.append(Text(bl + "─" * STATS_W + br, div))
    top = (h - len(box)) // 2
    empty = Text(" " * (STATS_W + 2), th.base_style)
    return [empty] * top + box + [empty] * (h - top - len(box))


def labelled_graph(values, w: int, h: int, th: Theme, grad: str, vmax: float, label: Text | None,
                   invert: bool = False) -> list[Text]:
    rows = graph_text(values, w, h, th.gradient(grad), 0.0, max(vmax, 1e-9), invert=invert)
    rows = [_fit(r, w, th) for r in rows]
    if label is not None and rows:
        idx = len(rows) - 1 if invert else 0
        rows[idx] = overlay(rows[idx], 0, label)
    return rows


def graph_area(st: DeviceState, th: Theme, w: int, h: int, util_only: bool = False) -> list[Text]:
    """util (top ~60 %) and power (bottom ~40 %) braille area graphs."""
    s = st.latest or _EMPTY
    info = st.info
    gt = th.style("graph_text")
    hist = st.history
    has_power = s.power_draw is not None and not util_only and h >= 2
    has_util = s.util_gpu is not None
    uh = h if not has_power else max(1, round(h * 0.6))
    ph = h - uh
    out: list[Text] = []
    if has_util:
        out += labelled_graph(hist["util_gpu"].tail(w * 2), w, uh, th, "util", 100.0, Text("util", gt))
    else:
        # No utilisation (e.g. Intel without perf access; the reason is shown in
        # the box's bottom border): the clock history stands in for it, since
        # it still reflects GPU activity.
        if s.clock_graphics is not None:
            vmax = float(info.clock_graphics_max or hist["clock_graphics"].max() or 1)
            out += labelled_graph(hist["clock_graphics"].tail(w * 2), w, uh, th, "util", vmax, Text("clk", gt))
        else:
            out += [Text(" " * w, th.base_style)] * uh
    if ph > 0:
        vmax = s.power_limit or info.power_limit_max or hist["power_draw"].max() or 1.0
        out += labelled_graph(hist["power_draw"].tail(w * 2), w, ph, th, "power", vmax, Text("power", gt))
    return out[:h]


# -- detail view boxes ----------------------------------------------------------------


class DetailBox(BoxWidget):
    """Base for the detail-view boxes; hidden while it has nothing to show."""

    metrics: tuple[str, ...] = ()

    def __init__(self, state: DeviceState, theme: Theme, **kwargs) -> None:
        super().__init__(theme, **kwargs)
        self.state = state

    def has_data(self) -> bool:
        if not is_live(self.state):  # off/suspended: no (stale) graphs
            return False
        return any(v is not None for m in self.metrics for v in self.state.history[m].tail(512))


class VramBox(DetailBox):
    """btop mem-box style: totals, a used meter and the used history graph."""

    color_key = "mem_box"
    metrics = ("mem_percent",)

    def top_tabs(self):
        return [Tab("vram")], None, []

    def bottom_tabs(self):
        s = self.state.latest or _EMPTY
        return [], ([Tab(f"{fmt.pct(s.mem_percent)} used", bold=False)] if s.mem_percent is not None else [])

    def content(self, w, h):
        th, s = self.gputop_theme, self.state.latest or _EMPTY
        title, main = th.style("title", bold=True), th.style("main_fg")
        rows = []

        def kv(k, v, ks, vs):
            t = Text(no_wrap=True)
            t.append(k, ks)
            t.append(v.rjust(max(0, w - len(k))), vs)
            return t

        rows.append(kv("Total:", fmt.gib(s.mem_total), title, title))
        rows.append(kv("Used:", fmt.gib(s.mem_used), main, main))
        if s.mem_percent is not None:
            t = Text(f"{fmt.pct(s.mem_percent):>4} ", main)
            t.append_text(btop_meter(s.mem_percent / 100, max(1, w - 5), th, "vram"))
            rows.append(t)
        if s.mem_total is not None and s.mem_used is not None:
            rows.append(kv("Free:", fmt.gib(s.mem_total - s.mem_used), main, main))
        rows = rows[:h]
        gh = h - len(rows)
        if gh > 0:
            rows += labelled_graph(self.state.history["mem_percent"].tail(w * 2), w, gh, th, "vram", 100.0, None)
        return rows


class PcieBox(DetailBox):
    """btop net-box style: download (RX) up, upload (TX) mirrored below."""

    color_key = "net_box"
    metrics = ("pcie_rx", "pcie_tx")

    def top_tabs(self):
        return [Tab("pcie")], None, []

    def bottom_tabs(self):
        s = self.state.latest or _EMPTY
        link = f"Gen{s.pcie_gen} x{s.pcie_width}" if s.pcie_gen and s.pcie_width else None
        return [], ([Tab(link, key="graph_text", bold=False)] if link else [])

    def content(self, w, h):
        th, st = self.gputop_theme, self.state
        s = st.latest or _EMPTY
        hist = st.history
        vmax = max(hist["pcie_rx"].max(w * 2) or 0, hist["pcie_tx"].max(w * 2) or 0, 1e6)
        main = th.style("main_fg")
        top_h = (h + 1) // 2
        bot_h = h - top_h
        rx_label = Text("▼ ", main)
        rx_label.append(fmt.rate(s.pcie_rx), th.style("title"))
        tx_label = Text("▲ ", main)
        tx_label.append(fmt.rate(s.pcie_tx), th.style("title"))
        rows = labelled_graph(hist["pcie_rx"].tail(w * 2), w, top_h, th, "rx", vmax, rx_label)
        if bot_h:
            rows += labelled_graph(hist["pcie_tx"].tail(w * 2), w, bot_h, th, "tx", vmax, tx_label, invert=True)
        scale = Text(fmt.rate(vmax), th.style("graph_text"))
        if rows and w > scale.cell_len + 16:
            rows[0] = overlay(rows[0], w - scale.cell_len, scale)
        return rows


class PowerTempBox(DetailBox):
    """Power and temperature history, stacked."""

    color_key = "cpu_box"
    metrics = ("power_draw", "temp_core")

    def top_tabs(self):
        return [Tab("power"), Tab("temp")], None, []

    def content(self, w, h):
        th, st = self.gputop_theme, self.state
        s, hist, info = st.latest or _EMPTY, st.history, st.info
        main = th.style("main_fg")
        has_p = any(v is not None for v in hist["power_draw"].tail(w * 2))
        has_t = any(v is not None for v in hist["temp_core"].tail(w * 2))
        ph = (h + 1) // 2 if has_p and has_t else (h if has_p else 0)
        rows: list[Text] = []
        if ph:
            vmax = s.power_limit or info.power_limit_max or hist["power_draw"].max() or 1.0
            lab = Text("power ", main)
            lab.append(fmt.power_pair(s.power_draw, s.power_limit), th.style("title"))
            rows += labelled_graph(hist["power_draw"].tail(w * 2), w, ph, th, "power", vmax, lab)
        if has_t and h - ph > 0:
            tmax = max(100.0, hist["temp_core"].max() or 0)
            lab = Text("temp ", main)
            lab.append(fmt.temp(s.temp_core), th.temp(s.temp_core) if s.temp_core is not None else main)
            rows += labelled_graph(hist["temp_core"].tail(w * 2), w, h - ph, th, "temp", tmax, lab)
        return rows


class GpuDetail(Container):
    """Single-GPU view: a big GPU box, then VRAM | PCIe | power/temp boxes."""

    DEFAULT_CSS = """
    GpuDetail { height: 1fr; layout: vertical; }
    GpuDetail > GpuPanel { height: 4fr; }
    GpuDetail > #detail-row { height: 3fr; }
    GpuDetail > #detail-row > DetailBox { width: 1fr; height: 1fr; }
    """

    def __init__(self, state: DeviceState, theme: Theme, hotkey: int | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = state
        self.gputop_theme = theme
        self.main = GpuPanel(state, theme, hotkey=hotkey, detail=True)
        self.boxes: list[DetailBox] = [VramBox(state, theme), PcieBox(state, theme), PowerTempBox(state, theme)]
        self._visible: tuple[bool, ...] | None = None

    def compose(self) -> ComposeResult:
        yield self.main
        with Horizontal(id="detail-row"):
            yield from self.boxes

    def on_mount(self) -> None:
        self._sync_boxes()

    def _sync_boxes(self) -> None:
        visible = tuple(b.has_data() for b in self.boxes)
        if visible != self._visible:
            self._visible = visible
            for box, show in zip(self.boxes, visible):
                box.display = show
            self.query_one("#detail-row").display = any(visible)

    def apply_theme(self, theme: Theme) -> None:
        self.gputop_theme = theme
        for w in [self.main, *self.boxes]:
            w.apply_theme(theme)

    def update_data(self) -> None:
        self._sync_boxes()
        for w in [self.main, *self.boxes]:
            if w.display:
                w.update_data()
