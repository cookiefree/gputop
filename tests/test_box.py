"""Box chrome compared against a real btop 1.4.8 capture (160x50, default config).

The reference lines below were captured from btop running in a pty (pyte), with
btop's battery widget removed from the top line since gputop has none.
"""

import pytest

from gputop.themes import Theme
from gputop.widgets.box import BoxStyle, Tab, box_bottom, box_lines, box_top, hotkey_tab, interval_tab

# btop's cpu box top border and proc box bottom border, verbatim from the capture.
BTOP_TOP = (
    "╭─┐¹cpu┌──┐menu┌┐preset *┌──────────────────────────────────────────────────┐13:30:10┌"
    "────────────────────────────────────────────────────────────┐- 1000ms +┌─╮"
)
BTOP_BOTTOM = "╰┘↑ select ↓└┘info ↵└┘terminate└┘kill└┘signals└┘Nice└┘Follow└───────────────────┘0/271└╯"


@pytest.fixture(autouse=True)
def rounded():
    BoxStyle.rounded = True
    yield
    BoxStyle.rounded = True


@pytest.fixture
def th():
    return Theme("default")


def btop_cpu_top(th, width=160):
    return box_top(width, th, "cpu_box",
                   [Tab("cpu", prefix="¹"), hotkey_tab("menu", gap=2), hotkey_tab("preset *", "p")],
                   Tab("13:30:10"), [interval_tab(1000)])


def colours(text):
    """Per-cell (char, colour hex, bold) for a rendered line."""
    out = []
    for i, ch in enumerate(text.plain):
        style = None
        for sp in text.spans:
            if sp.start <= i < sp.end:
                style = sp.style
        out.append((ch, style.color.triplet.hex if style and style.color else None, bool(style and style.bold)))
    return out


def test_top_border_matches_btop_exactly(th):
    assert btop_cpu_top(th).plain == BTOP_TOP


def test_square_corners(th):
    BoxStyle.rounded = False
    line = btop_cpu_top(th).plain
    assert line == "┌" + BTOP_TOP[1:-1] + "┐"
    assert box_bottom(20, th, "cpu_box").plain == "└" + "─" * 18 + "┘"


def test_bottom_border_matches_btop_exactly(th):
    labels = ["↑ select ↓", "info ↵", "terminate", "kill", "signals", "Nice", "Follow"]
    line = box_bottom(len(BTOP_BOTTOM), th, "proc_box", [Tab(t) for t in labels], [Tab("0/271")])
    assert line.plain == BTOP_BOTTOM


def test_top_border_colours_match_btop(th):
    """Colours sampled from the same btop capture (cells.json)."""
    cells = colours(btop_cpu_top(th))
    at = {i: c for i, c in enumerate(cells)}
    assert at[0] == ("╭", "#556d59", False)  # cpu_box border
    assert at[2] == ("┐", "#556d59", False)  # title bracket in the border colour
    assert at[3] == ("¹", "#b54040", True)  # superscript hotkey: hi_fg bold
    assert at[4] == ("c", "#eeeeee", True)  # box name: title bold
    assert at[11] == ("m", "#b54040", True)  # tab hotkey letter
    assert at[12] == ("e", "#eeeeee", True)
    assert at[77] == ("1", "#eeeeee", True)  # clock
    assert at[147] == ("-", "#b54040", True)  # "- 1000ms +": - and + in hi_fg
    assert at[149] == ("1", "#eeeeee", True)
    assert at[156] == ("+", "#b54040", True)


def test_long_titles_are_clipped_not_overlapping(th):
    line = box_top(40, th, "cpu_box", [Tab("gpu0", prefix="¹"), Tab("NVIDIA GeForce RTX 4090 Ti Super Duper")],
                   Tab("13:30:10"), [interval_tab(500)]).plain
    assert len(line) == 40
    assert line.startswith("╭─┐¹gpu0┌") and line.endswith("┐- 500ms +┌─╮")
    assert "13:30:10" not in line  # centre tab dropped rather than overlapping


def test_tiny_widths(th):
    assert box_top(1, th, "cpu_box").plain == "╭"
    assert box_top(2, th, "cpu_box").plain == "╭╮"
    assert len(box_top(5, th, "cpu_box", [Tab("a very long title")]).plain) == 5


def test_box_lines_frame_content(th):
    from rich.text import Text

    lines = box_lines(10, 4, th, "mem_box", [Text("hello world, too long")])
    assert [line.plain for line in lines] == [
        "╭────────╮",
        "│ hello  │",
        "│        │",
        "╰────────╯",
    ]
    assert lines[1].spans[0].style.color.triplet.hex == "#6c6c4b"
