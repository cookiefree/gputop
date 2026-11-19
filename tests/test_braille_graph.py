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
    """Raw sample -> dots mapping (smoothing tested separately below)."""
    kwargs.setdefault("smooth", False)
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
    rows = render_graph([10, 10, 90, 90], width=2, smooth=False)
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
    assert lines[0].plain == "⣠⣿"  # smoothed: the rise slopes
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


# -- filled, smoothed area graph ------------------------------------------------


def dot_heights(rows, invert=False):
    """Filled dots per dot column, decoded back from the braille characters,
    asserting every column is solid from the base up (no holes, no combing)."""
    height = len(rows)
    width = len(rows[0])
    cols = [0] * (width * 2)
    left_up, right_up = (0x40, 0x04, 0x02, 0x01), (0x80, 0x20, 0x10, 0x08)
    left_dn, right_dn = (0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80)
    base_first = range(height) if invert else range(height - 1, -1, -1)  # walk up from the base
    for r in base_first:
        level = r if invert else height - 1 - r
        for c, (ch, _) in enumerate(rows[r]):
            bits = 0 if ch == " " else ord(ch) - 0x2800
            for side, order in ((0, left_dn if invert else left_up), (1, right_dn if invert else right_up)):
                filled = [bool(bits & b) for b in order]
                n = sum(filled)
                assert filled == [True] * n + [False] * (4 - n), f"hole in cell {r},{c}: {ch!r}"
                if n:
                    assert cols[2 * c + side] == level * 4, f"gap below cell {r},{c}"
                    cols[2 * c + side] = level * 4 + n
    return cols


def test_filled_columns_have_no_holes():
    vals = [0, 100, 0, 37, 80, 80, 5, 60, 20, 90, 100, 0]
    for h in (1, 2, 5):
        for invert in (False, True):
            dot_heights(render_graph(vals, 6, h, invert=invert), invert)


def test_lone_spike_becomes_a_triangle_not_a_comb_tooth():
    vals = [0] * 5 + [100] + [0] * 6  # 12 samples -> 6 cells, height 2 (8 dots)
    raw = dot_heights(render_graph(vals, 6, 2, smooth=False))
    smooth = dot_heights(render_graph(vals, 6, 2))
    assert raw == [1, 1, 1, 1, 1, 8, 1, 1, 1, 1, 1, 1]  # one-dot-wide tooth
    assert smooth == [1, 1, 1, 1, 4, 8, 4, 1, 1, 1, 1, 1]  # neighbours slope up
    assert max(smooth) == 8  # the peak keeps its height


def test_rise_slopes_instead_of_stepping():
    vals = [0, 0, 0, 0, 100, 100, 100, 100]
    assert dot_heights(render_graph(vals, 4, 2)) == [1, 1, 1, 4, 8, 8, 8, 8]


def test_two_samples_per_cell():
    vals = list(range(0, 100, 5))  # 20 samples
    rows = render_graph(vals, 10, 3)
    assert all(len(r) == 10 for r in rows)
    heights = dot_heights(rows)
    assert len(heights) == 20 and heights == sorted(heights)


def test_smoothing_does_not_bridge_gaps():
    vals = [100, None, None, 100]
    assert dot_heights(render_graph(vals, 2, 1)) == [4, 0, 0, 4]


def test_vertical_gradient_bottom_to_top():
    rows = render_graph([100] * 4, 2, 4)
    positions = [row[0][1] for row in rows]
    assert positions == sorted(positions, reverse=True)  # top row = gradient end
    assert positions[0] == 1.0
