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


def test_vram_gradient_uses_btop_used_keys():
    t = Theme("default")
    assert t.gradient("vram")[0].color == t.gradient("used")[0].color
    assert t.gradient("vram")[0].color.triplet.hex == "#592b26"
    t2 = Theme("custom", {"vram_start": "#000000", "vram_end": "#ffffff"})
    assert t2.gradient("vram")[0].color != t2.gradient("used")[0].color


def test_default_theme_is_btops():
    t = Theme("default")
    expected = {
        "main_bg": "#000000", "main_fg": "#cccccc", "title": "#eeeeee", "hi_fg": "#b54040",
        "selected_bg": "#6a2f2f", "selected_fg": "#eeeeee", "inactive_fg": "#404040",
        "graph_text": "#606060", "meter_bg": "#404040", "div_line": "#303030", "proc_misc": "#0de756",
        "cpu_box": "#556d59", "mem_box": "#6c6c4b", "net_box": "#5c588d", "proc_box": "#805252",
        "temp_start": "#4897d4", "temp_mid": "#5474e8", "temp_end": "#ff40b6",
        "cpu_start": "#77ca9b", "cpu_mid": "#cbc06c", "cpu_end": "#dc4c4c",
        "used_start": "#592b26", "used_mid": "#d9626d", "used_end": "#ff4769",
        "free_start": "#384f21", "free_mid": "#b5e685", "free_end": "#dcff85",
        "cached_start": "#163350", "cached_mid": "#74e6fc", "cached_end": "#26c5ff",
        "available_start": "#4e3f0e", "available_mid": "#ffd77a", "available_end": "#ffb814",
        "download_start": "#291f75", "download_mid": "#4f43a3", "download_end": "#b0a9de",
        "upload_start": "#6e2143", "upload_mid": "#ac2f67", "upload_end": "#ff94c7",
        "process_start": "#80d0a3", "process_mid": "#dcd179", "process_end": "#d45454",
    }
    assert {k: t[k] for k in expected} == expected


def test_gradient_is_101_steps_through_mid():
    t = Theme("default")
    g = [s.color.triplet.hex for s in t.gradient("util")]
    assert len(g) == 101 and g[0] == "#77ca9b" and g[50] == "#cbc06c" and g[100] == "#dc4c4c"


def test_builtin_themes_are_btop_theme_files():
    from importlib import resources

    files = {f.name for f in resources.files("gputop").joinpath("theme_files").iterdir()}
    assert {"default.theme", "dracula.theme", "gruvbox.theme", "nord.theme"} <= files


def test_transparent_background_variant():
    t = Theme("default").with_background(False)
    assert t.base_style.bgcolor.is_default
    assert Theme("default").base_style.bgcolor.triplet.hex == "#000000"


def test_temperature_colour_scale():
    t = Theme("default")
    assert t.temp(0).color.triplet.hex == "#4897d4"
    assert t.temp(90).color.triplet.hex == "#ff40b6"  # 90 °C = 100 %
    assert t.temp(120).color == t.temp(90).color
