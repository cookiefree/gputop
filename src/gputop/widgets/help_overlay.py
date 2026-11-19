"""Help overlay listing key bindings (any key or click closes it)."""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Center, Middle
from textual.screen import ModalScreen

from ..themes import Theme
from .box import Tab
from .gpu_panel import BoxWidget

HELP_KEYS: tuple[tuple[str, str], ...] = (
    ("q / Esc", "quit"),
    ("Tab / Shift+Tab", "focus next / previous GPU"),
    ("1-9", "focus GPU by panel number (¹²³ in the title)"),
    ("a / Enter", "toggle all-GPUs overview / single-GPU detail"),
    ("↑ ↓ PgUp PgDn", "select process"),
    ("Home / End", "first / last process"),
    ("s", "cycle process sort column"),
    ("r", "reverse sort order"),
    ("f", "filter processes (Enter keeps, Esc clears)"),
    ("+ / -", "refresh interval faster / slower (100 ms - 5 s)"),
    ("t", "cycle colour theme"),
    ("h / ?", "this help"),
    ("", ""),
    ("mouse", "click panel: focus · double-click: detail view"),
    ("", "click column header: sort · wheel: scroll list"),
)


class HelpBox(BoxWidget):
    """The key list, drawn as a btop box (btop's help uses the same chrome)."""

    color_key = "div_line"
    pad = 2
    DEFAULT_CSS = """
    HelpBox { width: 74; max-width: 100%; }
    """

    def __init__(self, theme: Theme, extra: str = "", **kwargs) -> None:
        super().__init__(theme, **kwargs)
        self.extra = extra
        self.styles.height = len(HELP_KEYS) + 2 + (2 if extra else 0) + 2

    def top_tabs(self):
        return [Tab("help", hotkey=0)], None, []

    def bottom_tabs(self):
        return [Tab("press any key to close", key="graph_text", bold=False)], []

    def content(self, w, h):
        th = self.gputop_theme
        rows = [Text("")]
        for key, desc in HELP_KEYS:
            t = Text(no_wrap=True)
            t.append(f"{key:>16}  ", th.style("hi_fg", bold=True))
            t.append(desc, th.style("main_fg"))
            rows.append(t)
        if self.extra:
            rows += [Text(""), Text(self.extra, th.style("graph_text"))]
        return rows


class HelpOverlay(ModalScreen[None]):
    DEFAULT_CSS = """
    HelpOverlay { align: center middle; background: $background 40%; }
    """

    def __init__(self, theme: Theme, extra: str = "") -> None:
        super().__init__()
        self.gputop_theme = theme
        self.extra = extra

    def compose(self) -> ComposeResult:
        with Middle(), Center():
            yield HelpBox(self.gputop_theme, self.extra)

    def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        self.dismiss()

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.dismiss()
