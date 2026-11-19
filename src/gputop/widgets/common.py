"""Small helpers shared by widgets."""

from __future__ import annotations

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.strip import Strip
from textual.widget import Widget


def set_title(widget: Widget, attr: str, text: Text | None) -> None:
    """Assign ``border_title``/``border_subtitle`` only when it changed.

    Textual refreshes the widget on every assignment, so blindly setting
    titles from a refresh path would cause needless repaints (or loops).
    """
    cache_attr = f"_last_{attr}"
    key = None if text is None else (text.plain, tuple((s.start, s.end, str(s.style)) for s in text.spans))
    if getattr(widget, cache_attr, object()) != key:
        setattr(widget, cache_attr, key)
        setattr(widget, attr, text)


def reset_titles(widget: Widget) -> None:
    """Forget cached titles (after a theme change)."""
    for attr in ("border_title", "border_subtitle"):
        setattr(widget, f"_last_{attr}", object())


def join_lines(lines: list[Text], width: int) -> Text:
    """Join rendered lines, cropping each to ``width`` so nothing ever wraps."""
    out = Text(no_wrap=True, overflow="crop")
    for i, line in enumerate(lines):
        if i:
            out.append("\n")
        if line.cell_len > width:
            line = line.copy()
            line.truncate(width, overflow="crop")
        out.append_text(line)
    return out


def set_border(widget: Widget, color: str, kind: str = "round") -> None:
    """Set the border only when it changed; style writes can trigger a relayout."""
    key = (kind, color)
    if getattr(widget, "_last_border", None) != key:
        widget._last_border = key  # type: ignore[attr-defined]
        widget.styles.border = key


def text_to_segments(text: Text, default: Style | None = None) -> list[Segment]:
    """Convert a single-line Rich Text into segments without a Console.

    Much cheaper than letting Textual convert ``Text`` -> ``Content`` -> strips
    on every refresh, which dominated CPU use in profiling. Overlapping spans
    are combined in order, like Rich does.
    """
    plain = text.plain
    if not plain:
        return []
    base = text.style if isinstance(text.style, Style) and text.style else default
    spans = text.spans
    if not spans:
        return [Segment(plain, base)]
    bounds = {0, len(plain)}
    for sp in spans:
        bounds.add(max(0, min(len(plain), sp.start)))
        bounds.add(max(0, min(len(plain), sp.end)))
    edges = sorted(bounds)
    segments: list[Segment] = []
    for start, end in zip(edges, edges[1:]):
        if start == end:
            continue
        style = base
        for sp in spans:
            if sp.start <= start and sp.end >= end and isinstance(sp.style, Style):
                style = sp.style if style is None else style + sp.style
        if segments and segments[-1].style == style:
            segments[-1] = Segment(segments[-1].text + plain[start:end], style)
        else:
            segments.append(Segment(plain[start:end], style))
    return segments


class LinesWidget(Widget):
    """Widget rendered through Textual's Line API from ``render_text()``.

    Subclasses implement ``render_text()`` returning a (multi-line) Rich
    ``Text`` exactly as they would for ``render()``; this base converts it to
    strips once per refresh and fills unstyled cells with the theme colours.
    Subclasses must have a ``gputop_theme`` attribute.
    """

    _strips: list[Strip] | None = None

    def render_text(self) -> Text:  # pragma: no cover - abstract
        raise NotImplementedError

    def refresh(self, *args, **kwargs):  # type: ignore[override]
        self._strips = None
        return super().refresh(*args, **kwargs)

    def _build_strips(self) -> list[Strip]:
        w = self.content_size.width
        theme = self.gputop_theme  # type: ignore[attr-defined]
        base = theme.base_style
        text = self.render_text()
        strips = []
        for line in text.split("\n", allow_blank=True):
            strips.append(Strip(text_to_segments(line, base)).crop_extend(0, w, base))
        return strips

    def render_line(self, y: int) -> Strip:
        if self._strips is None:
            self._strips = self._build_strips()
        if 0 <= y < len(self._strips):
            return self._strips[y]
        return Strip.blank(self.content_size.width, self.gputop_theme.base_style)  # type: ignore[attr-defined]
