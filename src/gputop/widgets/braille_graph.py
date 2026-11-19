"""btop-style braille history graphs.

Each braille character is a 2x4 dot matrix, so one terminal cell holds two
samples horizontally and four levels vertically. A graph ``width`` cells wide
and ``height`` rows tall therefore shows the newest ``2 * width`` samples with
``4 * height`` levels of resolution.

The renderer (:func:`render_graph`) is pure and UI-free, which keeps it easy to
test; :class:`BrailleGraph` is a thin Textual widget around it.

Bit layout of a braille cell (Unicode U+2800 + bits)::

    dot1 0x01  dot4 0x08
    dot2 0x02  dot5 0x10
    dot3 0x04  dot6 0x20
    dot7 0x40  dot8 0x80
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from rich.style import Style
from rich.text import Text
from textual.widget import Widget

from ..themes import Theme
from .common import LinesWidget, join_lines

BRAILLE_BLANK = "⠀"
_BASE = 0x2800
# Dots filled from the bottom of the cell upwards.
_LEFT_UP = (0x40, 0x04, 0x02, 0x01)
_RIGHT_UP = (0x80, 0x20, 0x10, 0x08)
# Dots filled from the top downwards (for inverted graphs).
_LEFT_DOWN = (0x01, 0x02, 0x04, 0x40)
_RIGHT_DOWN = (0x08, 0x10, 0x20, 0x80)

# Precomputed codepoint for every (left_dots, right_dots, inverted) combo.
_CELL: dict[tuple[int, int, bool], str] = {}
for _inv in (False, True):
    _lt, _rt = (_LEFT_DOWN, _RIGHT_DOWN) if _inv else (_LEFT_UP, _RIGHT_UP)
    for _l in range(5):
        for _r in range(5):
            _bits = sum(_lt[:_l]) + sum(_rt[:_r])
            _CELL[(_l, _r, _inv)] = " " if _bits == 0 else chr(_BASE + _bits)

Cell = tuple[str, float]  # (character, colour position 0..1)


def braille_char(left: int, right: int, invert: bool = False) -> str:
    """Character with ``left``/``right`` (0-4) dots filled in each column.
    An empty cell renders as a plain space."""
    return _CELL[(max(0, min(4, left)), max(0, min(4, right)), invert)]


def _normalise(value: float | None, vmin: float, vmax: float) -> float | None:
    if value is None:
        return None
    span = vmax - vmin
    if span <= 0:
        return 1.0 if value > vmin else 0.0
    return max(0.0, min(1.0, (value - vmin) / span))


def render_graph(
    values: Sequence[float | None],
    width: int,
    height: int = 1,
    vmin: float = 0.0,
    vmax: float = 100.0,
    invert: bool = False,
    baseline: bool = True,
) -> list[list[Cell]]:
    """Render ``values`` (oldest first) into ``height`` rows of ``width`` cells.

    The newest sample is at the right edge; missing history on the left and
    ``None`` samples render as empty columns. With ``baseline`` every real
    sample shows at least one dot, so a flat 0% still draws a line (btop does
    the same).

    Each cell carries a colour position (0..1) for gradient colouring: for
    single-row graphs it is the value of that cell (so bars change colour with
    load); for taller graphs it is the row's height (btop's vertical
    green -> yellow -> red bands).

    ``invert`` draws from the top edge downwards (btop's upload graph).
    """
    if width <= 0 or height <= 0:
        return [[] for _ in range(max(0, height))]

    n = width * 2
    tail = list(values[-n:]) if len(values) > n else list(values)
    tail = [None] * (n - len(tail)) + tail
    total_dots = height * 4

    dots: list[int] = []
    norms: list[float] = []
    for v in tail:
        f = _normalise(v, vmin, vmax)
        if f is None:
            dots.append(0)
            norms.append(0.0)
            continue
        d = int(round(f * total_dots))
        if baseline and d == 0:
            d = 1
        dots.append(d)
        norms.append(f)

    rows: list[list[Cell]] = []
    for r in range(height):
        # Index of this row counted from the edge the graph grows from.
        level = r if invert else height - 1 - r
        offset = level * 4
        row: list[Cell] = []
        for c in range(width):
            dl = dots[2 * c] - offset
            dr = dots[2 * c + 1] - offset
            dl = 0 if dl < 0 else (4 if dl > 4 else dl)
            dr = 0 if dr < 0 else (4 if dr > 4 else dr)
            ch = _CELL[(dl, dr, invert)]
            if height == 1:
                pos = max(norms[2 * c], norms[2 * c + 1])
            else:
                pos = (level + 1) / height
            row.append((ch, pos))
        rows.append(row)
    return rows


def graph_to_strings(rows: list[list[Cell]]) -> list[str]:
    return ["".join(ch for ch, _ in row) for row in rows]


def graph_to_text(rows: list[list[Cell]], gradient: Sequence[Style]) -> list[Text]:
    """Colour rendered rows with a 101-step gradient (see Theme.gradient)."""
    last = len(gradient) - 1
    out = []
    for row in rows:
        text = Text(no_wrap=True, overflow="crop")
        # Merge runs of identical style to keep the segment count low.
        run_chars: list[str] = []
        run_style: Style | None = None
        for ch, pos in row:
            style = gradient[int(round(pos * last))] if ch != " " else None
            if style is not run_style and run_chars:
                text.append("".join(run_chars), run_style)
                run_chars = []
            run_style = style
            run_chars.append(ch)
        if run_chars:
            text.append("".join(run_chars), run_style)
        out.append(text)
    return out


def graph_text(
    values: Sequence[float | None],
    width: int,
    height: int,
    gradient: Sequence[Style],
    vmin: float = 0.0,
    vmax: float = 100.0,
    invert: bool = False,
) -> list[Text]:
    return graph_to_text(render_graph(values, width, height, vmin, vmax, invert), gradient)


class BrailleGraph(LinesWidget):
    """A standalone graph widget that fills its content area.

    ``source`` is called on every render and must return
    ``(values, vmin, vmax)``; the widget asks for nothing more than it can
    draw. Optional ``mirror_source`` draws a second, inverted graph in the
    bottom half (btop's net box style; used for PCIe RX/TX).
    """

    DEFAULT_CSS = """
    BrailleGraph { height: 1fr; width: 1fr; }
    """

    def __init__(
        self,
        source: Callable[[int], tuple[Sequence[float | None], float, float]],
        theme: Theme,
        gradient: str = "util",
        mirror_source: Callable[[int], tuple[Sequence[float | None], float, float]] | None = None,
        mirror_gradient: str = "tx",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.source = source
        self.mirror_source = mirror_source
        self.gradient_name = gradient
        self.mirror_gradient = mirror_gradient
        self.gputop_theme = theme

    def render_text(self) -> Text:
        w, h = self.content_size
        if w <= 0 or h <= 0:
            return Text("")
        lines: list[Text]
        if self.mirror_source is None:
            values, vmin, vmax = self.source(w * 2)
            lines = graph_text(values, w, h, self.gputop_theme.gradient(self.gradient_name), vmin, vmax)
        else:
            top_h = (h + 1) // 2
            bot_h = h - top_h
            values, vmin, vmax = self.source(w * 2)
            lines = graph_text(values, w, top_h, self.gputop_theme.gradient(self.gradient_name), vmin, vmax)
            if bot_h:
                mvals, mmin, mmax = self.mirror_source(w * 2)
                lines += graph_text(
                    mvals, w, bot_h, self.gputop_theme.gradient(self.mirror_gradient), mmin, mmax, invert=True
                )
        return join_lines(lines, w)
