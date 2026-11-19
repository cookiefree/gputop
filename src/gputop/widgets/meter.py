"""Gradient bar meters for instantaneous values (utilisation, VRAM, power).

Like btop, each filled cell is coloured by its *position* along the bar, so a
nearly full bar runs green -> yellow -> red while a low one stays green.
Partial cells use eighth-block characters for sub-cell precision.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.style import Style
from rich.text import Text
from textual.widget import Widget

from ..themes import Theme

FULL = "█"
EMPTY = "░"
EIGHTHS = ("", "▏", "▎", "▍", "▌", "▋", "▊", "▉")

MeterCell = tuple[str, float | None]  # (char, gradient position or None=empty)


def render_meter(fraction: float | None, width: int) -> list[MeterCell]:
    """Cells for a bar ``width`` wide filled to ``fraction`` (0..1).

    ``None`` (unsupported value) renders an all-empty bar.
    """
    if width <= 0:
        return []
    f = 0.0 if fraction is None else max(0.0, min(1.0, fraction))
    eighths_total = int(round(f * width * 8))
    full, rem = divmod(eighths_total, 8)
    cells: list[MeterCell] = []
    denom = max(1, width - 1)
    for i in range(width):
        pos = i / denom
        if i < full:
            cells.append((FULL, pos))
        elif i == full and rem:
            cells.append((EIGHTHS[rem], pos))
        else:
            cells.append((EMPTY, None))
    return cells


def meter_to_text(cells: Sequence[MeterCell], gradient: Sequence[Style], empty: Style) -> Text:
    text = Text(no_wrap=True, overflow="crop")
    last = len(gradient) - 1
    for ch, pos in cells:
        text.append(ch, empty if pos is None else gradient[int(round(pos * last))])
    return text


def meter_text(fraction: float | None, width: int, theme: Theme, gradient: str = "util") -> Text:
    return meter_to_text(render_meter(fraction, width), theme.gradient(gradient), theme.style("meter_bg"))


class Meter(Widget):
    """One-line meter widget filling its width."""

    DEFAULT_CSS = "Meter { height: 1; }"

    def __init__(self, theme: Theme, gradient: str = "util", fraction: float | None = 0.0, **kwargs) -> None:
        super().__init__(**kwargs)
        self.gputop_theme = theme
        self.gradient_name = gradient
        self._fraction = fraction

    @property
    def fraction(self) -> float | None:
        return self._fraction

    @fraction.setter
    def fraction(self, value: float | None) -> None:
        if value != self._fraction:
            self._fraction = value
            self.refresh()

    def render(self) -> Text:
        return meter_text(self._fraction, self.content_size.width, self.gputop_theme, self.gradient_name)
