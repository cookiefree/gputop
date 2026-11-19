"""Help overlay listing key bindings (any key or click closes it)."""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Center, Middle
from textual.screen import ModalScreen
from textual.widget import Widget

from ..themes import Theme

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


class HelpBox(Widget):
    DEFAULT_CSS = """
    HelpBox {
        width: 72;
        height: auto;
        max-width: 100%;
        border: round white;
        border-title-align: center;
        border-subtitle-align: center;
        padding: 1 2;
    }
    """

    def __init__(self, theme: Theme, extra: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.gputop_theme = theme
        self.extra = extra

    def on_mount(self) -> None:
        th = self.gputop_theme
        self.styles.border = ("round", th["hi_fg"])
        self.styles.background = th["main_bg"]
        self.border_title = Text("gputop help", th.style("title", bold=True))
        self.border_subtitle = Text("press any key to close", th.style("graph_text"))

    def get_content_height(self, container, viewport, width: int) -> int:
        return len(HELP_KEYS) + (2 if self.extra else 0)

    def render(self) -> Text:
        th = self.gputop_theme
        out = Text(no_wrap=True, overflow="ellipsis")
        for i, (key, desc) in enumerate(HELP_KEYS):
            if i:
                out.append("\n")
            out.append(f"{key:>16}  ", th.style("hi_fg", bold=True))
            out.append(desc, th.style("main_fg"))
        if self.extra:
            out.append("\n\n")
            out.append(self.extra, th.style("graph_text"))
        return out


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
