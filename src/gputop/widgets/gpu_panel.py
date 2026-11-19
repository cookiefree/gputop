"""Per-GPU widgets: the compact boxed panel (overview) and the detail view."""

from __future__ import annotations

from collections.abc import Callable

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Container
from textual.events import Click
from textual.message import Message
from textual.widget import Widget

from .. import fmt
from ..backends.base import THROTTLE_LABELS, THROTTLE_WARNING, GPUSample
from ..sampler import DeviceState
from ..themes import Theme
from .braille_graph import BrailleGraph, graph_text
from .common import LinesWidget, join_lines, reset_titles, set_border, set_title
from .meter import meter_text

LABEL_W = 6


def throttle_tags(sample: GPUSample | None) -> tuple[list[str], bool]:
    """(short labels, any_warning) for a sample's throttle reasons."""
    if sample is None or not sample.throttle_reasons:
        return [], False
    reasons = sample.throttle_reasons
    warn = [THROTTLE_LABELS.get(r, r.upper()) for r in reasons if r in THROTTLE_WARNING]
    if warn:
        return warn, True
    return [THROTTLE_LABELS.get(r, r.upper()) for r in reasons], False


def gpu_title(state: DeviceState, theme: Theme, hotkey: int | None, focused: bool) -> Text:
    t = Text()
    if hotkey is not None and hotkey <= 9:
        t.append(fmt.superscript(hotkey), theme.style("hi_fg"))
    t.append(f"GPU {state.index}", theme.style("title", bold=True))
    t.append(" · ", theme.style("graph_text"))
    t.append(fmt.short_gpu_name(state.info.name), theme.style("hi_fg" if focused else "title"))
    return t


def status_subtitle(state: DeviceState, theme: Theme) -> Text | None:
    if state.lost:
        return Text("LOST", theme.style("hi_fg", bold=True))
    if state.error:
        return Text("error", theme.style("hi_fg"))
    tags, warn = throttle_tags(state.latest)
    if not tags:
        return None
    if warn:
        return Text("⚠ " + " ".join(tags), theme.grad("util", 1.0))
    return Text(" ".join(tags).lower(), theme.style("inactive_fg"))


class GpuPanel(LinesWidget):
    """A btop-style boxed summary of one GPU, sized to whatever space it gets.

    Rows by priority (the first ones always fit; the rest appear as the panel
    grows, and any leftover height makes the graphs taller):
    util graph, VRAM meter, status (temp/fan/clock), power graph,
    PCIe, memory-controller + codec, clocks.
    """

    DEFAULT_CSS = """
    GpuPanel {
        border: round white;
        border-title-align: left;
        border-subtitle-align: right;
        padding: 0 1;
        height: 6;
        width: 1fr;
    }
    """

    class Selected(Message):
        def __init__(self, index: int, chain: int) -> None:
            super().__init__()
            self.index = index
            self.chain = chain

    def __init__(self, state: DeviceState, theme: Theme, hotkey: int | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = state
        self.gputop_theme = theme
        self.hotkey = hotkey
        self.selected = False

    # -- styling --------------------------------------------------------------

    def apply_theme(self, theme: Theme) -> None:
        self.gputop_theme = theme
        reset_titles(self)
        self.update_chrome()
        self.refresh()

    def set_selected(self, selected: bool) -> None:
        if selected != self.selected:
            self.selected = selected
            self.update_chrome()

    def update_chrome(self) -> None:
        th = self.gputop_theme
        if self.state.lost:
            color = th["inactive_fg"]
        elif self.selected:
            color = th["hi_fg"]
        else:
            color = th["cpu_box"]
        set_border(self, color)
        set_title(self, "border_title", gpu_title(self.state, th, self.hotkey, self.selected))
        set_title(self, "border_subtitle", status_subtitle(self.state, th))

    def on_mount(self) -> None:
        self.update_chrome()

    def update_data(self) -> None:
        self.update_chrome()
        self.refresh()

    def on_click(self, event: Click) -> None:
        event.stop()
        self.post_message(self.Selected(self.state.index, getattr(event, "chain", 1)))

    # -- rendering ------------------------------------------------------------

    def _plan_rows(self, s: GPUSample, h: int) -> tuple[list[str], int, int]:
        available = {
            "util": True,
            "vram": True,
            "status": True,
            "power": s.power_draw is not None,
            "pcie": s.pcie_rx is not None or s.pcie_tx is not None,
            "mctl": s.util_mem is not None or s.enc_util is not None or s.dec_util is not None,
            "clocks": s.clock_graphics is not None or s.clock_mem is not None,
        }
        priority = ["util", "vram", "status", "power", "pcie", "mctl", "clocks"]
        chosen = [r for r in priority if available[r]][: max(1, h)]
        order = ["util", "vram", "power", "status", "mctl", "pcie", "clocks"]
        rows = [r for r in order if r in chosen]
        extra = max(0, h - len(rows))
        if "power" in rows:
            util_h = 1 + extra - extra // 2
            power_h = 1 + extra // 2
        else:
            util_h, power_h = 1 + extra, 1
        return rows, util_h, power_h

    def render_text(self) -> Text:
        w, h = self.content_size
        th = self.gputop_theme
        st = self.state
        if w <= 0 or h <= 0:
            return Text("")
        if st.lost:
            msg = Text("GPU no longer available", th.style("hi_fg", bold=True), justify="center")
            pad = Text("\n" * max(0, (h - 1) // 2))
            return pad + msg
        s = st.latest
        if s is None:
            return Text("waiting for data…", th.style("inactive_fg"))

        label = th.style("main_fg")
        value = th.style("title")
        dim = th.style("graph_text")

        util_v = fmt.pct(s.util_gpu)
        vram_v = fmt.mem_pair(s.mem_used, s.mem_total)
        pwr_v = fmt.power_pair(s.power_draw, s.power_limit)
        vw = max(len(util_v), len(vram_v), len(pwr_v) if s.power_draw is not None else 0)
        bar_w = max(1, w - LABEL_W - 1 - vw)

        rows, util_h, power_h = self._plan_rows(s, h)
        lines: list[Text] = []

        def graph_rows(name: str, values, gh: int, vmax: float, grad: str, val: str) -> None:
            g = graph_text(values, bar_w, gh, th.gradient(grad), 0.0, vmax)
            for i, row in enumerate(g):
                line = Text(no_wrap=True, overflow="crop")
                line.append((name if i == 0 else "").ljust(LABEL_W), label)
                line.append_text(row)
                line.append(" " + (val.rjust(vw) if i == 0 else " " * vw), value)
                lines.append(line)

        for r in rows:
            if r == "util":
                graph_rows("util", st.history["util_gpu"].tail(bar_w * 2), util_h, 100.0, "util", util_v)
            elif r == "vram":
                line = Text(no_wrap=True, overflow="crop")
                line.append("vram".ljust(LABEL_W), label)
                frac = None if s.mem_percent is None else s.mem_percent / 100
                line.append_text(meter_text(frac, bar_w, th, "vram"))
                line.append(" " + vram_v.rjust(vw), value)
                lines.append(line)
            elif r == "power":
                vmax = s.power_limit or st.info.power_limit_max or st.history["power_draw"].max() or 1.0
                graph_rows("power", st.history["power_draw"].tail(bar_w * 2), power_h, vmax, "power", pwr_v)
            elif r == "status":
                lines.append(self._status_line(s, w, label, value, dim))
            elif r == "mctl":
                parts = []
                if s.util_mem is not None:
                    parts.append(("mctl ", s.util_mem))
                if s.enc_util is not None:
                    parts.append(("enc ", s.enc_util))
                if s.dec_util is not None:
                    parts.append(("dec ", s.dec_util))
                line = Text(no_wrap=True, overflow="crop")
                for i, (name, v) in enumerate(parts):
                    if i:
                        line.append("  ")
                    line.append(name, label)
                    line.append(fmt.pct(v), th.grad("util", v / 100))
                lines.append(line)
            elif r == "pcie":
                line = Text(no_wrap=True, overflow="crop")
                line.append("pcie".ljust(LABEL_W), label)
                line.append("↓", th.grad("rx", 1.0))
                line.append(fmt.rate(s.pcie_rx), value)
                line.append("  ↑", th.grad("tx", 1.0))
                line.append(fmt.rate(s.pcie_tx), value)
                if s.pcie_gen is not None and s.pcie_width is not None:
                    link = f"  Gen{s.pcie_gen}x{s.pcie_width}"
                    if line.cell_len + len(link) <= w:
                        line.append(link, dim)
                lines.append(line)
            elif r == "clocks":
                line = Text(no_wrap=True, overflow="crop")
                line.append("clk".ljust(LABEL_W), label)
                gmax = st.info.clock_graphics_max
                if s.clock_graphics is not None:
                    line.append(f"{s.clock_graphics}", value)
                    if gmax:
                        line.append(f"/{gmax}", dim)
                    line.append(" MHz", dim)
                if s.clock_mem is not None:
                    line.append("  mem ", label)
                    line.append(f"{s.clock_mem}", value)
                    line.append(" MHz", dim)
                lines.append(line)
        return join_lines(lines[:h], w)

    def _status_line(self, s: GPUSample, w: int, label: Style, value: Style, dim: Style) -> Text:
        th = self.gputop_theme
        segments: list[list[tuple[str, Style]]] = []
        if s.temp_core is not None:
            segments.append([("temp ", label), (fmt.temp(s.temp_core), th.grad("temp", s.temp_core / 100))])
        if s.temp_mem is not None:
            segments.append([("mem ", label), (fmt.temp(s.temp_mem), th.grad("temp", s.temp_mem / 100))])
        if s.fan is not None:
            segments.append([("fan ", label), (fmt.pct(s.fan), th.grad("temp", s.fan / 100))])
        if s.clock_graphics is not None:
            segments.append([(f"{s.clock_graphics}", value), ("MHz", dim)])
        line = Text(no_wrap=True, overflow="crop")
        used = 0
        for seg in segments:
            seg_len = sum(len(t) for t, _ in seg) + (2 if used else 0)
            if used + seg_len > w:
                # Drop memory temperature first when space is tight.
                continue
            if used:
                line.append("  ")
            for t, st in seg:
                line.append(t, st)
            used += seg_len
        return line


class MetricBox(BrailleGraph):
    """A boxed, titled history graph used by the detail view."""

    DEFAULT_CSS = """
    MetricBox {
        border: round white;
        border-title-align: left;
        border-subtitle-align: right;
        height: 1fr;
        width: 1fr;
    }
    """

    def __init__(self, title_fn: Callable[[], tuple[Text, Text | None]], *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.title_fn = title_fn

    def apply_theme(self, theme: Theme) -> None:
        self.gputop_theme = theme
        reset_titles(self)
        self.update_chrome()
        self.refresh()

    def update_chrome(self) -> None:
        set_border(self, self.gputop_theme["cpu_box"])
        title, subtitle = self.title_fn()
        set_title(self, "border_title", title)
        set_title(self, "border_subtitle", subtitle)

    def on_mount(self) -> None:
        self.update_chrome()

    def update_data(self) -> None:
        self.update_chrome()
        self.refresh()


class GpuInfoBar(LinesWidget):
    """Static facts + headline numbers at the top of the detail view."""

    DEFAULT_CSS = "GpuInfoBar { height: 2; padding: 0 1; }"

    def __init__(self, state: DeviceState, theme: Theme, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = state
        self.gputop_theme = theme

    def render_text(self) -> Text:
        th = self.gputop_theme
        st = self.state
        info = st.info
        s = st.latest
        label, value, dim = th.style("main_fg"), th.style("title"), th.style("graph_text")
        sep = (" · ", dim)

        first: list[tuple[str, Style]] = [(info.name, th.style("title", bold=True))]
        if info.short_uuid:
            first += [sep, (info.short_uuid, dim)]
        if info.driver_version:
            first += [sep, ("driver ", label), (info.driver_version, value)]
        if s is not None and s.pcie_gen is not None:
            link = f"Gen{s.pcie_gen} x{s.pcie_width}" if s.pcie_width else f"Gen{s.pcie_gen}"
            first += [sep, ("PCIe ", label), (link, value)]
            if info.pcie_gen_max:
                first.append((f" (max Gen{info.pcie_gen_max} x{info.pcie_width_max})", dim))
        if info.memory_total:
            first += [sep, (fmt.bytes_short(info.memory_total) + " VRAM", value)]

        second: list[tuple[str, Style]] = []
        if st.lost:
            second = [("GPU no longer available: " + (st.error or ""), th.style("hi_fg", bold=True))]
        elif s is None:
            second = [("waiting for data…", th.style("inactive_fg"))]
        else:
            tags, warn = throttle_tags(s)
            if tags:
                second += [("throttle ", label),
                           (" ".join(tags), th.grad("util", 1.0) if warn else th.style("inactive_fg"))]
            else:
                second += [("throttle ", label), ("none", dim)]
            if s.enc_util is not None or s.dec_util is not None:
                second += [sep, ("enc ", label), (fmt.pct(s.enc_util), value),
                           ("  dec ", label), (fmt.pct(s.dec_util), value)]
            nprocs = len(st.processes)
            second += [sep, (f"{nprocs} process{'es' if nprocs != 1 else ''}", value)]
            if st.error:
                second += [sep, (st.error, th.style("hi_fg"))]

        lines = []
        for parts in (first, second):
            line = Text(no_wrap=True)
            for t, style in parts:
                line.append(t, style)
            lines.append(line)
        return join_lines(lines, self.content_size.width)


class GpuDetail(Container):
    """Single-GPU view: info bar plus six large metric graphs."""

    DEFAULT_CSS = """
    GpuDetail { height: 1fr; layout: vertical; }
    GpuDetail > #graphs { layout: grid; grid-size: 3 2; height: 1fr; }
    """

    def __init__(self, state: DeviceState, theme: Theme, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = state
        self.gputop_theme = theme

    # Value providers: each returns (values, vmin, vmax) for n samples.
    def _hist(self, metric: str, vmax_fn: Callable[[], float]):
        def source(n: int):
            return self.state.history[metric].tail(n), 0.0, max(1e-9, vmax_fn())
        return source

    def compose(self) -> ComposeResult:
        st, th = self.state, self.gputop_theme
        info = st.info
        yield GpuInfoBar(st, th)

        def latest() -> GPUSample:
            return st.latest or GPUSample(index=st.index)

        def T(*parts: tuple[str, str]) -> Text:  # noqa: N802 - tiny builder
            t = Text()
            for txt, key in parts:
                t.append(txt, self.gputop_theme.style(key, bold=(key == "title")))
            return t

        def util_title():
            s = latest()
            sub = T(("mctl ", "main_fg"), (fmt.pct(s.util_mem), "title")) if s.util_mem is not None else None
            return T(("util ", "main_fg"), (fmt.pct(s.util_gpu), "title")), sub

        def vram_title():
            s = latest()
            return T(("vram ", "main_fg"), (fmt.mem_pair(s.mem_used, s.mem_total), "title")), \
                T((fmt.pct(s.mem_percent), "title"))

        def power_vmax() -> float:
            s = latest()
            return s.power_limit or info.power_limit_max or st.history["power_draw"].max() or 1.0

        def power_title():
            s = latest()
            sub = T((fmt.pct(s.power_percent), "title")) if s.power_percent is not None else None
            return T(("power ", "main_fg"), (fmt.power_pair(s.power_draw, s.power_limit), "title")), sub

        def temp_title():
            s = latest()
            t = T(("temp ", "main_fg"), (fmt.temp(s.temp_core), "title"))
            if s.temp_mem is not None:
                t.append_text(T(("  mem ", "main_fg"), (fmt.temp(s.temp_mem), "title")))
            if s.temp_hotspot is not None:
                t.append_text(T(("  hot ", "main_fg"), (fmt.temp(s.temp_hotspot), "title")))
            sub = T(("fan ", "main_fg"), (fmt.pct(s.fan), "title")) if s.fan is not None else None
            return t, sub

        def clock_vmax() -> float:
            return float(info.clock_graphics_max or st.history["clock_graphics"].max() or 1)

        def clock_title():
            s = latest()
            t = T(("gfx ", "main_fg"), (fmt.mhz(s.clock_graphics), "title"))
            if info.clock_graphics_max:
                t.append(f" / {info.clock_graphics_max}", self.gputop_theme.style("graph_text"))
            sub = T(("mem ", "main_fg"), (fmt.mhz(s.clock_mem), "title")) if s.clock_mem is not None else None
            return t, sub

        def pcie_vmax() -> float:
            # Shared autoscale for RX and TX so the two halves are comparable.
            m = max(st.history["pcie_rx"].max(240) or 0, st.history["pcie_tx"].max(240) or 0)
            return max(m, 1e6)

        def pcie_title():
            s = latest()
            t = T(("pcie ", "main_fg"), ("↓", "download_end"), (fmt.rate(s.pcie_rx), "title"),
                  ("  ↑", "upload_end"), (fmt.rate(s.pcie_tx), "title"))
            sub = T((f"Gen{s.pcie_gen} x{s.pcie_width}", "graph_text")) if s.pcie_gen else None
            return t, sub

        with Container(id="graphs"):
            yield MetricBox(util_title, self._hist("util_gpu", lambda: 100.0), th, "util")
            yield MetricBox(vram_title, self._hist("mem_percent", lambda: 100.0), th, "vram")
            yield MetricBox(power_title, self._hist("power_draw", power_vmax), th, "power")
            yield MetricBox(temp_title, self._hist("temp_core", lambda: max(100.0, st.history["temp_core"].max() or 0)), th, "temp")
            yield MetricBox(clock_title, self._hist("clock_graphics", clock_vmax), th, "util")
            yield MetricBox(pcie_title, self._hist("pcie_rx", pcie_vmax), th, "rx",
                            mirror_source=self._hist("pcie_tx", pcie_vmax), mirror_gradient="tx")

    def set_columns(self, cols: int) -> None:
        graphs = self.query_one("#graphs")
        rows = (6 + cols - 1) // cols
        graphs.styles.grid_size_columns = cols
        graphs.styles.grid_size_rows = rows

    def apply_theme(self, theme: Theme) -> None:
        self.gputop_theme = theme
        for w in self.query(MetricBox):
            w.apply_theme(theme)
        for w in self.query(GpuInfoBar):
            w.gputop_theme = theme
            w.refresh()

    def update_data(self) -> None:
        for w in self.query(MetricBox):
            w.update_data()
        for w in self.query(GpuInfoBar):
            w.refresh()
