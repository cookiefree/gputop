"""Config file at ``$XDG_CONFIG_HOME/gputop/config.toml`` (default ``~/.config``).

Only settings changed interactively (theme, interval, sort) are written back
on exit, so a one-off ``--theme`` on the command line doesn't stick.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "gputop"


def config_path() -> Path:
    return config_dir() / "config.toml"


@dataclass
class Config:
    interval_ms: int = 500
    theme: str = "default"
    mouse: bool = True
    backend: str = "auto"  # auto, nvidia, intel, amd, fake, or a list: "nvidia,intel"
    sort_key: str = "vram"
    sort_reverse: bool = False
    view: str = "all"  # "all" or "detail"
    color: str = "auto"  # auto, truecolor, 256, 16
    rounded_corners: bool = True  # btop's default; false draws square corners
    theme_background: bool = True  # false: keep the terminal's own background

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        """Load config; missing file or bad values fall back to defaults."""
        path = path or config_path()
        cfg = cls()
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cfg
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            cfg._load_error = f"ignoring {path}: {exc}"  # type: ignore[attr-defined]
            return cfg
        for f in fields(cls):
            if f.name in data:
                value = data[f.name]
                default = getattr(cfg, f.name)
                if isinstance(default, bool):
                    if isinstance(value, bool):
                        setattr(cfg, f.name, value)
                elif isinstance(default, int):
                    if isinstance(value, int) and not isinstance(value, bool):
                        setattr(cfg, f.name, value)
                elif isinstance(value, str):
                    setattr(cfg, f.name, value)
        cfg.interval_ms = max(100, min(5000, cfg.interval_ms))
        if cfg.view not in ("all", "detail"):
            cfg.view = "all"
        return cfg

    def to_toml(self) -> str:
        lines = ["# gputop configuration", ""]
        for key, value in asdict(self).items():
            if isinstance(value, bool):
                lines.append(f"{key} = {'true' if value else 'false'}")
            elif isinstance(value, int):
                lines.append(f"{key} = {value}")
            else:
                escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{key} = "{escaped}"')
        return "\n".join(lines) + "\n"

    def save(self, path: Path | None = None) -> None:
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".toml.tmp")
        tmp.write_text(self.to_toml(), encoding="utf-8")
        tmp.replace(path)

    def save_changes(self, changes: dict[str, object], path: Path | None = None) -> None:
        """Merge ``changes`` into the on-disk config (not CLI overrides)."""
        if not changes:
            return
        on_disk = Config.load(path)
        for key, value in changes.items():
            if hasattr(on_disk, key):
                setattr(on_disk, key, value)
        on_disk.save(path)
