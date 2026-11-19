"""btop-style box chrome: borders with titles embedded in them.

Matches btop's own drawing (verified against a capture of btop 1.4)::

    ╭─┐¹cpu┌──┐menu┌┐preset *┌──────────┐13:30:10┌───────────┐- 1000ms +┌─╮
    │ ...                                                                 │
    ╰┘↑ select ↓└┘info ↵└────────────────────────────────────────┘0/271└╯

* Top-border titles are wrapped in ``┐ … ┌``, bottom-border ones in ``┘ … └``.
* The hotkey letter of a tab is drawn in ``hi_fg``; a superscript digit before
  a box name is the box's number key.
* Corners are rounded (btop's default, ``rounded_corners = true``) or square.
* The border colour of a box is fixed; state is shown in titles and tags.

Everything here is pure and works on a cell buffer, so overlapping/too-long
titles are clipped deterministically (see :func:`box_top`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.style import Style
from rich.text import Text

from ..themes import Theme

ROUND = ("╭", "╮", "╰", "╯")
SQUARE = ("┌", "┐", "└", "┘")
H, V = "─", "│"
TOP_OPEN, TOP_CLOSE = "┐", "┌"
BOT_OPEN, BOT_CLOSE = "┘", "└"


class BoxStyle:
    """Process-wide corner style (set from the config at startup)."""

    rounded: bool = True

    @classmethod
    def corners(cls) -> tuple[str, str, str, str]:
        return ROUND if cls.rounded else SQUARE


@dataclass
class Tab:
    """One title segment drawn inside a border: ``┐text┌`` / ``┘text└``.

    ``hotkey``: index into ``text`` of the character drawn in ``hi_fg``.
    ``prefix``: drawn before ``text`` in ``hi_fg`` bold (superscript digits).
    ``key``: theme colour key for the text. ``selected``: selected_bg/fg.
    ``rich``: a prebuilt Text used instead of ``text`` (for coloured values).
    ``gap``: border cells to leave before this tab.
    """

    text: str = ""
    hotkey: int | None = None
    prefix: str = ""
    key: str = "title"
    bold: bool = True
    selected: bool = False
    rich: Text | None = None
    gap: int = 0
    marks: dict[int, str] = field(default_factory=dict)  # index -> colour key

    def render(self, theme: Theme) -> Text:
        if self.rich is not None:
            return self.rich
        t = Text(no_wrap=True)
        if self.prefix:
            t.append(self.prefix, theme.style("hi_fg", bold=True))
        if self.selected:
            base = Style(color=theme["selected_fg"], bgcolor=theme["selected_bg"], bold=True)
        else:
            base = theme.style(self.key, bold=self.bold)
        for i, ch in enumerate(self.text):
            if i == self.hotkey:
                style = theme.style("hi_fg", bold=self.bold)
                if self.selected:
                    style = Style(color=theme["hi_fg"], bgcolor=theme["selected_bg"], bold=True)
            elif i in self.marks:
                style = theme.style(self.marks[i], bold=self.bold)
            else:
                style = base
            t.append(ch, style)
        return t

    def width(self, theme: Theme) -> int:
        return self.render(theme).cell_len + 2  # + brackets


def hotkey_tab(text: str, key_char: str | None = None, **kwargs) -> Tab:
    """Tab whose hotkey is the first occurrence of ``key_char`` (default: first letter)."""
    k = key_char if key_char is not None else (text[:1] or None)
    idx = text.find(k) if k else -1
    return Tab(text, hotkey=idx if idx >= 0 else None, **kwargs)


class _Line:
    """A fixed-width row of (char, style) cells."""

    def __init__(self, width: int, char: str, style: Style) -> None:
        self.chars = [char] * width
        self.styles = [style] * width

    def put(self, x: int, text: Text) -> None:
        for span_text, style in _cells(text):
            if 0 <= x < len(self.chars):
                self.chars[x] = span_text
                self.styles[x] = style
            x += 1

    def to_text(self) -> Text:
        out = Text(no_wrap=True)
        run: list[str] = []
        run_style: Style | None = None
        for ch, st in zip(self.chars, self.styles):
            if st is not run_style and run:
                out.append("".join(run), run_style)
                run = []
            run_style = st
            run.append(ch)
        if run:
            out.append("".join(run), run_style)
        return out


def _cells(text: Text) -> list[tuple[str, Style]]:
    """Split a Text into per-character (char, style) cells."""
    plain = text.plain
    styles: list[Style | None] = [text.style if isinstance(text.style, Style) else None] * len(plain)
    for sp in text.spans:
        if not isinstance(sp.style, Style):
            continue
        for i in range(max(0, sp.start), min(len(plain), sp.end)):
            styles[i] = sp.style if styles[i] is None else styles[i] + sp.style
    return [(ch, st or Style()) for ch, st in zip(plain, styles)]


def _bracketed(tab: Tab, theme: Theme, border: Style, top: bool) -> Text:
    o, c = (TOP_OPEN, TOP_CLOSE) if top else (BOT_OPEN, BOT_CLOSE)
    t = Text(no_wrap=True)
    t.append(o, border)
    t.append_text(tab.render(theme))
    t.append(c, border)
    return t


def _edge(width: int, theme: Theme, color_key: str, top: bool, left: list[Tab],
          center: Tab | None, right: list[Tab]) -> Text:
    tl, tr, bl, br = BoxStyle.corners()
    border = theme.style(color_key)
    if width <= 0:
        return Text("")
    if width == 1:
        return Text(tl if top else bl, border)
    line = _Line(width, H, border)
    line.chars[0], line.chars[-1] = (tl, tr) if top else (bl, br)
    lead = 1 if top else 0  # btop: "╭─┐title┌" but "╰┘hint└"
    trail = 1 if top else 0  # "┐- 500ms +┌─╮" but "┘0/271└╯"
    limit_l, limit_r = 1, width - 1  # usable cells between the corners

    # Right group first (it is anchored), dropping tabs that don't fit.
    rendered_right = [_bracketed(t, theme, border, top) for t in right]
    while rendered_right and sum(r.cell_len for r in rendered_right) + trail > (limit_r - limit_l) // 2:
        rendered_right.pop(0)
    x = limit_r - trail - sum(r.cell_len for r in rendered_right)
    right_start = x
    for r in rendered_right:
        line.put(x, r)
        x += r.cell_len

    # Left group: as many tabs as fit before the right group.
    x = limit_l + lead
    left_end = x
    for tab in left:
        r = _bracketed(tab, theme, border, top)
        x += tab.gap
        if x + r.cell_len > right_start - 1:
            # Clip the tab (with an ellipsis) if a useful part of it still fits.
            if right_start - 1 - x > 5:
                clipped = Text(no_wrap=True)
                clipped.append_text(r)
                clipped.truncate(right_start - 1 - x - 1, overflow="ellipsis")
                clipped.append(TOP_CLOSE if top else BOT_CLOSE, border)
                line.put(x, clipped)
                left_end = x + clipped.cell_len
            break
        line.put(x, r)
        x += r.cell_len
        left_end = x

    # Centre tab (the clock) only where it doesn't collide with the others.
    if center is not None:
        r = _bracketed(center, theme, border, top)
        # btop: x + width/2 - len(text)/2 for the opening bracket.
        cx = width // 2 - (r.cell_len - 2) // 2
        if cx > left_end and cx + r.cell_len < right_start:
            line.put(cx, r)
    return line.to_text()


def box_top(width: int, theme: Theme, color_key: str, left: list[Tab] = (), center: Tab | None = None,
            right: list[Tab] = ()) -> Text:
    return _edge(width, theme, color_key, True, list(left), center, list(right))


def box_bottom(width: int, theme: Theme, color_key: str, left: list[Tab] = (),
               right: list[Tab] = ()) -> Text:
    return _edge(width, theme, color_key, False, list(left), None, list(right))


def box_lines(width: int, height: int, theme: Theme, color_key: str, content: list[Text],
              top: Text | None = None, bottom: Text | None = None, pad: int = 1) -> list[Text]:
    """A complete box: top border, ``content`` rows framed by ``│``, bottom border.

    Content rows are cropped/padded to ``width - 2 - 2*pad`` cells.
    """
    border = theme.style(color_key)
    tl, tr, bl, br = BoxStyle.corners()
    if height <= 0 or width <= 0:
        return []
    lines = [top if top is not None else box_top(width, theme, color_key)]
    inner = max(0, width - 2 - 2 * pad)
    blank = Text(" " * inner, theme.base_style)
    for i in range(max(0, height - 2)):
        row = content[i] if i < len(content) else blank
        if row.cell_len > inner:
            row = row.copy()
            row.truncate(inner, overflow="crop")
        line = Text(no_wrap=True)
        line.append(V, border)
        if pad:
            line.append(" " * pad, theme.base_style)
        line.append_text(row)
        if row.cell_len < inner:
            line.append(" " * (inner - row.cell_len), theme.base_style)
        if pad:
            line.append(" " * pad, theme.base_style)
        line.append(V, border)
        lines.append(line)
    if height >= 2:
        lines.append(bottom if bottom is not None else box_bottom(width, theme, color_key))
    return lines[:height]


def interval_tab(ms: int) -> Tab:
    """btop's ``┐- 1000ms +┌`` with the - and + in hi_fg."""
    text = f"- {ms}ms +"
    return Tab(text, marks={0: "hi_fg", len(text) - 1: "hi_fg"})
