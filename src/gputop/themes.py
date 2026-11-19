"""Colour themes in btop's ``.theme`` format.

btop's theme format is the source of truth: the built-in themes are plain
``.theme`` files shipped in ``gputop/theme_files/`` (``default.theme`` is btop's
own Default theme), and any btop theme file works unchanged. User themes go in
``~/.config/gputop/themes/``::

    theme[main_bg]="#1e1e2e"
    theme[cpu_start]="#a6e3a1"

A TOML variant (``[colors]`` table with the same keys) is also accepted.
Colour values may be ``#rrggbb``, ``#gg`` (greyscale, btop shorthand) or
``"r g b"`` decimal. Missing keys fall back to ``default.theme``.

How gputop uses btop's keys:

=============  =======================  =====================================
gradient       btop key prefix          used for
=============  =======================  =====================================
util           ``cpu``                  GPU utilisation, enc/dec, mctl, clocks
power          ``power`` -> ``cpu``     power meter and graph
vram           ``vram`` -> ``used``     VRAM meter and graph
temp           ``temp``                 temperatures, fan
rx / tx        ``download``/``upload``  PCIe RX / TX
proc           ``process``              per-process VRAM and SM%
free/cached/   same names               (available for themes; btop parity)
available
=============  =======================  =====================================

Boxes: ``cpu_box`` (GPU boxes), ``mem_box`` (VRAM), ``net_box`` (PCIe),
``proc_box`` (processes), ``div_line`` (inner dividers).
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from functools import cached_property
from importlib import resources
from pathlib import Path

from rich.color import Color
from rich.style import Style

GRADIENTS: dict[str, tuple[str, ...]] = {
    # gputop gradient name -> btop key prefixes to try, in order
    "util": ("cpu",),
    "power": ("power", "cpu"),
    "vram": ("vram", "used"),
    "temp": ("temp",),
    "rx": ("download",),
    "tx": ("upload",),
    "proc": ("process",),
    "free": ("free",),
    "cached": ("cached",),
    "available": ("available",),
    "used": ("used",),
}

BUILTIN_ORDER = ("default", "dracula", "gruvbox", "nord")

# Temperatures are coloured 0 °C .. TEMP_MAX along the temp gradient (btop
# maps 0..100 % of the throttle temperature the same way).
TEMP_MAX = 90.0

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


def parse_btop_colors(text: str, name: str = "theme") -> dict[str, str]:
    colors: dict[str, str] = {}
    for line in text.splitlines():
        m = _THEME_LINE.match(line)
        if m:
            raw = m.group(2) if m.group(2) is not None else m.group(3)
            color = parse_color(raw)
            if color:
                colors[m.group(1)] = color
    if not colors:
        raise ThemeError(f"{name}: no theme[...] entries found")
    return colors


def _builtin_text(name: str) -> str:
    return resources.files("gputop").joinpath("theme_files", f"{name}.theme").read_text(encoding="utf-8")


DEFAULT_COLORS: dict[str, str] = parse_btop_colors(_builtin_text("default"), "default")
BUILTIN_THEMES: dict[str, dict[str, str]] = {
    name: parse_btop_colors(_builtin_text(name), name) for name in BUILTIN_ORDER
}


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _lerp(a: str, b: str, t: float) -> str:
    ra, ga, ba = _hex_to_rgb(a)
    rb, gb, bb = _hex_to_rgb(b)
    return "#{:02x}{:02x}{:02x}".format(
        round(ra + (rb - ra) * t), round(ga + (gb - ga) * t), round(ba + (bb - ba) * t)
    )


def build_gradient(start: str, mid: str | None, end: str, steps: int = 101) -> list[str]:
    """``steps`` colours: linear RGB start -> mid (0-50 %), mid -> end (50-100 %)."""
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


def blend(a: str, b: str, t: float) -> str:
    """Mix colour ``a`` towards ``b`` by ``t`` (0..1)."""
    return _lerp(a, b, max(0.0, min(1.0, t)))


@dataclass
class Theme:
    name: str
    colors: dict[str, str] = field(default_factory=dict)
    source: str = "builtin"
    background: bool = True  # False: leave the terminal's own background

    def __post_init__(self) -> None:
        merged = dict(DEFAULT_COLORS)
        merged.update({k: v for k, v in self.colors.items() if v})
        self.colors = merged

    def with_background(self, background: bool) -> Theme:
        if background == self.background:
            return self
        return Theme(self.name, dict(self.colors), self.source, background)

    def __getitem__(self, key: str) -> str:
        return self.colors.get(key) or DEFAULT_COLORS.get(key, "#cccccc")

    @cached_property
    def bg(self) -> Color:
        """Background for every cell; the terminal default when disabled."""
        return Color.parse(self["main_bg"]) if self.background else Color.default()

    @cached_property
    def base_style(self) -> Style:
        """Default foreground on the theme background (for unstyled cells)."""
        return Style(color=self["main_fg"], bgcolor=self.bg)

    def style(self, key: str, bold: bool = False) -> Style:
        cache = self.__dict__.setdefault("_styles", {})
        k = (key, bold)
        if k not in cache:
            # Styles carry the background so rendered cells never need a
            # second pass to paint it (see widgets.common.LinesWidget).
            cache[k] = Style(color=self[key], bgcolor=self.bg, bold=bold)
        return cache[k]

    def color_style(self, color: str, bold: bool = False) -> Style:
        """Style for an arbitrary ``#rrggbb`` colour on the theme background."""
        cache = self.__dict__.setdefault("_cstyles", {})
        k = (color, bold)
        if k not in cache:
            cache[k] = Style(color=color, bgcolor=self.bg, bold=bold)
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
            out[name] = [Style(color=c, bgcolor=self.bg) for c in build_gradient(start, mid, end)]
        return out

    def gradient(self, name: str) -> list[Style]:
        """101 Rich styles from 0% to 100% for gradient ``name``."""
        return self._gradients.get(name) or self._gradients["util"]

    def grad(self, name: str, fraction: float) -> Style:
        """Style at ``fraction`` (0..1) along gradient ``name``."""
        steps = self.gradient(name)
        idx = int(round(max(0.0, min(1.0, fraction)) * (len(steps) - 1)))
        return steps[idx]

    def temp(self, celsius: float) -> Style:
        """Temperature text colour (0 °C .. TEMP_MAX along the temp gradient)."""
        return self.grad("temp", celsius / TEMP_MAX)


def parse_theme_text(text: str, name: str, fmt: str) -> Theme:
    if fmt == "toml":
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ThemeError(f"{name}: {exc}") from exc
        name = str(data.get("name", name))
        table = data.get("colors", data)
        colors = {}
        for key, value in table.items():
            if isinstance(value, str) and key != "name":
                color = parse_color(value)
                if color:
                    colors[key] = color
        return Theme(name=name, colors=colors)
    return Theme(name=name, colors=parse_btop_colors(text, name))


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
