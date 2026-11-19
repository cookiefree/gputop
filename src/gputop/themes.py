"""Colour themes.

Themes use **btop's colour keys**, so existing btop ``.theme`` files load
unchanged. Two file formats are accepted from ``~/.config/gputop/themes/``:

btop style (``mytheme.theme``)::

    theme[main_bg]="#1e1e2e"
    theme[cpu_start]="#a6e3a1"

TOML (``mytheme.toml``)::

    [colors]
    main_bg = "#1e1e2e"
    cpu_start = "#a6e3a1"

Colour values may be ``#rrggbb``, ``#gg`` (greyscale, btop shorthand) or
``"r g b"`` decimal. Missing keys fall back to the ``default`` theme.

Gradients used by gputop (each has ``_start``, ``_mid`` and ``_end``):

=========  =====================  ==========================================
gputop     btop key prefix        used for
=========  =====================  ==========================================
util       ``cpu``                utilisation graphs, clocks, meters
vram       ``vram`` -> ``cpu``    VRAM meter/graph (own key optional)
power      ``power`` -> ``cpu``   power meter/graph (own key optional)
temp       ``temp``               temperatures, fan
rx / tx    ``download``/``upload``  PCIe throughput
proc       ``process``            per-process SM% bars
=========  =====================  ==========================================
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from rich.style import Style

GRADIENTS: dict[str, tuple[str, ...]] = {
    # gputop gradient name -> btop key prefixes to try, in order
    "util": ("cpu",),
    "vram": ("vram", "cpu"),
    "power": ("power", "cpu"),
    "temp": ("temp",),
    "rx": ("download",),
    "tx": ("upload",),
    "proc": ("process",),
}

DEFAULT_COLORS: dict[str, str] = {
    "main_bg": "#000000",
    "main_fg": "#cccccc",
    "title": "#eeeeee",
    "hi_fg": "#b54040",
    "selected_bg": "#6a2f2f",
    "selected_fg": "#eeeeee",
    "inactive_fg": "#404040",
    "graph_text": "#606060",
    "meter_bg": "#404040",
    "proc_misc": "#0de756",
    "cpu_box": "#3d7b46",
    "mem_box": "#8a882e",
    "net_box": "#423ba5",
    "proc_box": "#923535",
    "div_line": "#303030",
    "temp_start": "#4897d4",
    "temp_mid": "#5474e8",
    "temp_end": "#ff40b6",
    "cpu_start": "#50f095",
    "cpu_mid": "#f2e266",
    "cpu_end": "#fa1e1e",
    "download_start": "#291f75",
    "download_mid": "#4f43a3",
    "download_end": "#b0a9de",
    "upload_start": "#620665",
    "upload_mid": "#7d4180",
    "upload_end": "#dcafde",
    "process_start": "#80d0a3",
    "process_mid": "#dcd179",
    "process_end": "#d45454",
}

BUILTIN_THEMES: dict[str, dict[str, str]] = {
    "default": DEFAULT_COLORS,
    "dracula": {
        "main_bg": "#282a36",
        "main_fg": "#f8f8f2",
        "title": "#f8f8f2",
        "hi_fg": "#ff79c6",
        "selected_bg": "#44475a",
        "selected_fg": "#f8f8f2",
        "inactive_fg": "#6272a4",
        "graph_text": "#6272a4",
        "meter_bg": "#44475a",
        "proc_misc": "#bd93f9",
        "cpu_box": "#bd93f9",
        "mem_box": "#50fa7b",
        "net_box": "#ff5555",
        "proc_box": "#8be9fd",
        "div_line": "#44475a",
        "temp_start": "#8be9fd",
        "temp_mid": "#bd93f9",
        "temp_end": "#ff79c6",
        "cpu_start": "#50fa7b",
        "cpu_mid": "#f1fa8c",
        "cpu_end": "#ff5555",
        "download_start": "#6272a4",
        "download_mid": "#bd93f9",
        "download_end": "#d6acff",
        "upload_start": "#6272a4",
        "upload_mid": "#ff79c6",
        "upload_end": "#ff92df",
        "process_start": "#50fa7b",
        "process_mid": "#f1fa8c",
        "process_end": "#ff5555",
    },
    "gruvbox": {
        "main_bg": "#282828",
        "main_fg": "#ebdbb2",
        "title": "#fbf1c7",
        "hi_fg": "#fe8019",
        "selected_bg": "#504945",
        "selected_fg": "#fbf1c7",
        "inactive_fg": "#665c54",
        "graph_text": "#a89984",
        "meter_bg": "#3c3836",
        "proc_misc": "#8ec07c",
        "cpu_box": "#98971a",
        "mem_box": "#d79921",
        "net_box": "#458588",
        "proc_box": "#cc241d",
        "div_line": "#504945",
        "temp_start": "#83a598",
        "temp_mid": "#d3869b",
        "temp_end": "#fb4934",
        "cpu_start": "#b8bb26",
        "cpu_mid": "#fabd2f",
        "cpu_end": "#fb4934",
        "download_start": "#458588",
        "download_mid": "#83a598",
        "download_end": "#8ec07c",
        "upload_start": "#b16286",
        "upload_mid": "#d3869b",
        "upload_end": "#fe8019",
        "process_start": "#8ec07c",
        "process_mid": "#fabd2f",
        "process_end": "#fb4934",
    },
    "nord": {
        "main_bg": "#2e3440",
        "main_fg": "#d8dee9",
        "title": "#eceff4",
        "hi_fg": "#88c0d0",
        "selected_bg": "#434c5e",
        "selected_fg": "#eceff4",
        "inactive_fg": "#4c566a",
        "graph_text": "#7b88a1",
        "meter_bg": "#3b4252",
        "proc_misc": "#8fbcbb",
        "cpu_box": "#5e81ac",
        "mem_box": "#81a1c1",
        "net_box": "#88c0d0",
        "proc_box": "#81a1c1",
        "div_line": "#4c566a",
        "temp_start": "#5e81ac",
        "temp_mid": "#b48ead",
        "temp_end": "#bf616a",
        "cpu_start": "#a3be8c",
        "cpu_mid": "#ebcb8b",
        "cpu_end": "#bf616a",
        "download_start": "#5e81ac",
        "download_mid": "#81a1c1",
        "download_end": "#88c0d0",
        "upload_start": "#b48ead",
        "upload_mid": "#d08770",
        "upload_end": "#ebcb8b",
        "process_start": "#a3be8c",
        "process_mid": "#ebcb8b",
        "process_end": "#bf616a",
    },
}

_THEME_LINE = re.compile(r'^\s*theme\[(\w+)\]\s*=\s*(?:"([^"]*)"|([^\s"]+))')


class ThemeError(ValueError):
    pass


def parse_color(value: str) -> str | None:
    """Normalise a btop colour value to ``#rrggbb``; ``""`` -> ``None``."""
    v = value.strip().strip('"').strip()
    if not v:
        return None
    if v.startswith("#"):
        hexpart = v[1:]
        if re.fullmatch(r"[0-9A-Fa-f]{2}", hexpart):
            return "#" + hexpart.lower() * 3
        if re.fullmatch(r"[0-9A-Fa-f]{6}", hexpart):
            return "#" + hexpart.lower()
        if re.fullmatch(r"[0-9A-Fa-f]{3}", hexpart):
            return "#" + "".join(c * 2 for c in hexpart.lower())
        raise ThemeError(f"invalid colour {value!r}")
    parts = v.replace(",", " ").split()
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        r, g, b = (min(255, int(p)) for p in parts)
        return f"#{r:02x}{g:02x}{b:02x}"
    raise ThemeError(f"invalid colour {value!r}")


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _lerp(a: str, b: str, t: float) -> str:
    ra, ga, ba = _hex_to_rgb(a)
    rb, gb, bb = _hex_to_rgb(b)
    return "#{:02x}{:02x}{:02x}".format(
        round(ra + (rb - ra) * t), round(ga + (gb - ga) * t), round(ba + (bb - ba) * t)
    )


def build_gradient(start: str, mid: str | None, end: str, steps: int = 101) -> list[str]:
    """``steps`` colours from start -> (mid) -> end, like btop's gradients."""
    out = []
    for i in range(steps):
        t = i / (steps - 1)
        if mid is None:
            out.append(_lerp(start, end, t))
        elif t <= 0.5:
            out.append(_lerp(start, mid, t * 2))
        else:
            out.append(_lerp(mid, end, (t - 0.5) * 2))
    return out


@dataclass
class Theme:
    name: str
    colors: dict[str, str] = field(default_factory=dict)
    source: str = "builtin"

    def __post_init__(self) -> None:
        merged = dict(DEFAULT_COLORS)
        merged.update({k: v for k, v in self.colors.items() if v})
        self.colors = merged

    def __getitem__(self, key: str) -> str:
        return self.colors.get(key) or DEFAULT_COLORS.get(key, "#cccccc")

    @cached_property
    def base_style(self) -> Style:
        """Default foreground on the theme background (for unstyled cells)."""
        return Style(color=self["main_fg"], bgcolor=self["main_bg"])

    def style(self, key: str, bold: bool = False) -> Style:
        return self._style_cache(key, bold)

    def _style_cache(self, key: str, bold: bool) -> Style:
        cache = self.__dict__.setdefault("_styles", {})
        k = (key, bold)
        if k not in cache:
            # Styles carry the theme background so rendered cells never need
            # a second pass to paint it (see widgets.common.LinesWidget).
            cache[k] = Style(color=self[key], bgcolor=self["main_bg"], bold=bold)
        return cache[k]

    def _gradient_keys(self, name: str) -> tuple[str, str | None, str]:
        for prefix in GRADIENTS.get(name, (name,)):
            start = self.colors.get(f"{prefix}_start")
            end = self.colors.get(f"{prefix}_end")
            if start and end:
                return start, self.colors.get(f"{prefix}_mid"), end
        return self["cpu_start"], self.colors.get("cpu_mid"), self["cpu_end"]

    @cached_property
    def _gradients(self) -> dict[str, list[Style]]:
        out: dict[str, list[Style]] = {}
        for name in GRADIENTS:
            start, mid, end = self._gradient_keys(name)
            bg = self["main_bg"]
            out[name] = [Style(color=c, bgcolor=bg) for c in build_gradient(start, mid, end)]
        return out

    def gradient(self, name: str) -> list[Style]:
        """101 Rich styles from 0% to 100% for gradient ``name``."""
        return self._gradients.get(name) or self._gradients["util"]

    def grad(self, name: str, fraction: float) -> Style:
        """Style at ``fraction`` (0..1) along gradient ``name``."""
        steps = self.gradient(name)
        idx = int(round(max(0.0, min(1.0, fraction)) * (len(steps) - 1)))
        return steps[idx]


def parse_theme_text(text: str, name: str, fmt: str) -> Theme:
    colors: dict[str, str] = {}
    if fmt == "toml":
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ThemeError(f"{name}: {exc}") from exc
        name = str(data.get("name", name))
        table = data.get("colors", data)
        for key, value in table.items():
            if isinstance(value, str) and key != "name":
                color = parse_color(value)
                if color:
                    colors[key] = color
    else:
        for line in text.splitlines():
            m = _THEME_LINE.match(line)
            if m:
                raw = m.group(2) if m.group(2) is not None else m.group(3)
                color = parse_color(raw)
                if color:
                    colors[m.group(1)] = color
        if not colors:
            raise ThemeError(f"{name}: no theme[...] entries found")
    return Theme(name=name, colors=colors)


def load_theme_file(path: str | Path) -> Theme:
    p = Path(path).expanduser()
    fmt = "toml" if p.suffix.lower() == ".toml" else "btop"
    theme = parse_theme_text(p.read_text(encoding="utf-8"), p.stem, fmt)
    theme.source = str(p)
    return theme


def user_theme_dir(config_dir: Path) -> Path:
    return config_dir / "themes"


def available_themes(config_dir: Path | None = None) -> dict[str, Theme]:
    """Built-in themes plus any valid files in ``<config_dir>/themes``."""
    themes = {name: Theme(name, dict(colors)) for name, colors in BUILTIN_THEMES.items()}
    if config_dir is not None:
        d = user_theme_dir(config_dir)
        if d.is_dir():
            for f in sorted(d.iterdir()):
                if f.suffix.lower() in (".theme", ".toml") and f.is_file():
                    try:
                        t = load_theme_file(f)
                    except (OSError, ThemeError, UnicodeDecodeError):
                        continue
                    themes[f.stem] = t
    return themes


def get_theme(name_or_path: str, config_dir: Path | None = None) -> Theme:
    """Look a theme up by name, or load it from a path."""
    themes = available_themes(config_dir)
    if name_or_path in themes:
        return themes[name_or_path]
    p = Path(name_or_path).expanduser()
    if p.suffix and p.is_file():
        return load_theme_file(p)
    raise ThemeError(
        f"unknown theme {name_or_path!r}; available: {', '.join(sorted(themes))}"
    )
