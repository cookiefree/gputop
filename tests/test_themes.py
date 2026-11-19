import pytest

from gputop.themes import (
    BUILTIN_THEMES,
    Theme,
    ThemeError,
    available_themes,
    build_gradient,
    get_theme,
    parse_color,
    parse_theme_text,
)


def test_parse_color_formats():
    assert parse_color("#AABBCC") == "#aabbcc"
    assert parse_color("#40") == "#404040"
    assert parse_color("#abc") == "#aabbcc"
    assert parse_color("255 128 0") == "#ff8000"
    assert parse_color("") is None
    with pytest.raises(ThemeError):
        parse_color("#12345")
    with pytest.raises(ThemeError):
        parse_color("red")


def test_builtin_themes_present():
    assert {"default", "dracula", "gruvbox", "nord"} <= set(BUILTIN_THEMES)


def test_gradient_endpoints():
    g = build_gradient("#000000", "#808080", "#ffffff")
    assert len(g) == 101
    assert g[0] == "#000000" and g[50] == "#808080" and g[100] == "#ffffff"


def test_btop_theme_file_parses():
    text = '''
# a btop theme
theme[main_bg]="#1e1e2e"
theme[main_fg]="#cd"
theme[cpu_start]="166 227 161"
theme[cpu_end]="#f38ba8"   # trailing comment
theme[title]=#ffffff
'''
    t = parse_theme_text(text, "mocha", "btop")
    assert t["main_bg"] == "#1e1e2e"
    assert t["main_fg"] == "#cdcdcd"
    assert t["cpu_start"] == "#a6e3a1"
    assert t["title"] == "#ffffff"
    # unspecified keys fall back to default
    assert t["proc_box"] == BUILTIN_THEMES["default"]["proc_box"]


def test_toml_theme_parses():
    text = 'name = "x"\n[colors]\nmain_bg = "#101010"\ntemp_start = "#00ff00"\n'
    t = parse_theme_text(text, "file", "toml")
    assert t.name == "x"
    assert t["main_bg"] == "#101010"


def test_empty_btop_theme_rejected():
    with pytest.raises(ThemeError):
        parse_theme_text("nothing here", "bad", "btop")


def test_user_themes_dir(tmp_path):
    (tmp_path / "themes").mkdir()
    (tmp_path / "themes" / "mine.theme").write_text('theme[main_bg]="#123456"\n')
    (tmp_path / "themes" / "broken.toml").write_text("this is = = not toml")
    themes = available_themes(tmp_path)
    assert "mine" in themes and themes["mine"]["main_bg"] == "#123456"
    assert "broken" not in themes
    assert get_theme("mine", tmp_path)["main_bg"] == "#123456"
    with pytest.raises(ThemeError):
        get_theme("nope", tmp_path)


def test_vram_gradient_falls_back_to_cpu():
    t = Theme("default")
    assert t.gradient("vram")[0].color == t.gradient("util")[0].color
    t2 = Theme("custom", {"vram_start": "#000000", "vram_end": "#ffffff"})
    assert t2.gradient("vram")[0].color != t2.gradient("util")[0].color
