import pytest

from gputop.themes import Theme
from gputop.widgets.braille_graph import (
    braille_char,
    graph_text,
    graph_to_strings,
    render_graph,
)
from gputop.widgets.meter import EMPTY, FULL, meter_text, render_meter


def strings(*args, **kwargs):
    return graph_to_strings(render_graph(*args, **kwargs))


# -- braille_char ------------------------------------------------------------


def test_braille_char_levels_bottom_up():
    assert braille_char(0, 0) == " "
    assert braille_char(1, 1) == "⣀"
    assert braille_char(2, 2) == "⣤"
    assert braille_char(3, 3) == "⣶"
    assert braille_char(4, 4) == "⣿"
    assert braille_char(4, 0) == "⡇"
    assert braille_char(0, 4) == "⢸"
    assert braille_char(1, 2) == "⣠"


def test_braille_char_inverted_top_down():
    assert braille_char(1, 1, invert=True) == "⠉"
    assert braille_char(4, 4, invert=True) == "⣿"
    assert braille_char(2, 0, invert=True) == "⠃"


def test_braille_char_clamps():
    assert braille_char(-3, 9) == braille_char(0, 4)


# -- render_graph ------------------------------------------------------------


def test_single_row_levels():
    # 4 dots of resolution: 0, 25, 50, 75, 100 %
    assert strings([25, 50, 75, 100], width=2) == ["⣠⣾"]


def test_two_samples_per_cell_and_right_alignment():
    # Fewer samples than columns: history is right aligned, left is blank.
    assert strings([100, 100], width=3) == ["  ⣿"]


def test_only_newest_samples_are_shown():
    vals = [0] * 10 + [100, 100, 100, 100]
    assert strings(vals, width=2) == ["⣿⣿"]


def test_baseline_for_zero_but_gap_for_none():
    assert strings([0, 0], width=1) == ["⣀"]
    assert strings([None, None], width=1) == [" "]
    assert strings([0, 0], width=1, baseline=False) == [" "]
    assert strings([None, 0], width=1) == ["⢀"]


def test_multi_row_fill():
    # height 2 -> 8 levels. 50% fills exactly the bottom row.
    assert strings([50, 50], width=1, height=2) == [" ", "⣿"]
    assert strings([100, 100], width=1, height=2) == ["⣿", "⣿"]
    # 62.5% -> 5 dots: full bottom cell + 1 dot in the top cell
    assert strings([62.5, 62.5], width=1, height=2) == ["⣀", "⣿"]


def test_inverted_graph_grows_down():
    assert strings([50, 50], width=1, height=2, invert=True) == ["⣿", " "]
    assert strings([25, 25], width=1, invert=True) == ["⠉"]


def test_custom_range_and_clamping():
    assert strings([150, 150], width=1, vmin=50, vmax=150) == ["⣿"]
    assert strings([500, -20], width=1) == ["⣇"]
    # Values outside the range clamp; below vmin still shows the baseline.
    assert strings([-20, -20], width=1) == ["⣀"]


def test_zero_span_range():
    assert strings([5, 0], width=1, vmin=0, vmax=0) == ["⣇"]


def test_dimensions():
    rows = render_graph(list(range(100)), width=7, height=3)
    assert len(rows) == 3
    assert all(len(r) == 7 for r in rows)
    assert render_graph([1, 2], width=0, height=2) == [[], []]


def test_colour_positions_single_row_follow_value():
    rows = render_graph([10, 10, 90, 90], width=2)
    (_, low), (_, high) = rows[0]
    assert low == pytest.approx(0.1)
    assert high == pytest.approx(0.9)


def test_colour_positions_multi_row_follow_height():
    rows = render_graph([100, 100], width=1, height=4)
    positions = [row[0][1] for row in rows]
    assert positions == [1.0, 0.75, 0.5, 0.25]  # top row is "hottest"


def test_graph_text_applies_gradient():
    theme = Theme("default")
    lines = graph_text([0, 0, 100, 100], 2, 1, theme.gradient("util"))
    assert lines[0].plain == "⣀⣿"
    spans = lines[0].spans
    assert len(spans) == 2
    assert spans[0].style.color != spans[1].style.color


# -- meter -------------------------------------------------------------------


def test_meter_fill_and_partial():
    cells = render_meter(0.5, 4)
    assert "".join(c for c, _ in cells) == FULL * 2 + EMPTY * 2
    cells = render_meter(0.5625, 4)  # 2.25 cells -> 2 full + quarter block
    assert "".join(c for c, _ in cells) == FULL * 2 + "▎" + EMPTY


def test_meter_bounds():
    assert "".join(c for c, _ in render_meter(0, 3)) == EMPTY * 3
    assert "".join(c for c, _ in render_meter(1.7, 3)) == FULL * 3
    assert "".join(c for c, _ in render_meter(None, 3)) == EMPTY * 3
    assert render_meter(0.5, 0) == []


def test_meter_gradient_positions():
    cells = render_meter(1.0, 5)
    assert [p for _, p in cells] == [0.0, 0.25, 0.5, 0.75, 1.0]
    assert all(p is None for _, p in render_meter(0.0, 5))


def test_meter_text_width():
    t = meter_text(0.3, 12, Theme("default"))
    assert len(t.plain) == 12
