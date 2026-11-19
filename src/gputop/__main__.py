"""Command-line entry point: ``gputop`` / ``python -m gputop``."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from . import __version__
from .backends import BACKEND_NAMES, BackendError, GPUBackend, create_backend
from .config import Config, config_dir
from .sampler import Sampler

COLOR_SYSTEMS = {"truecolor": "truecolor", "256": "256", "16": "standard"}


def _gpu_list(value: str) -> list[int]:
    try:
        indices = [int(v) for v in value.split(",") if v.strip() != ""]
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected comma-separated GPU indices, got {value!r}") from None
    if not indices or any(i < 0 for i in indices):
        raise argparse.ArgumentTypeError(f"expected comma-separated GPU indices, got {value!r}")
    return indices


def _interval(value: str) -> int:
    try:
        ms = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"interval must be an integer number of ms, got {value!r}") from None
    if not 100 <= ms <= 5000:
        raise argparse.ArgumentTypeError("interval must be between 100 and 5000 ms")
    return ms


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gputop",
        description="btop-style multi-GPU terminal monitor.",
        epilog="Keys: h or ? inside the app. Config: ~/.config/gputop/config.toml",
    )
    p.add_argument("--interval", type=_interval, metavar="MS", help="refresh interval, 100-5000 ms (default 500)")
    p.add_argument("--theme", metavar="NAME", help="theme name or path to a .theme/.toml file")
    p.add_argument("--gpu", type=_gpu_list, metavar="INDEX[,INDEX...]", help="only show these GPUs")
    p.add_argument("--no-mouse", action="store_true", help="disable mouse support")
    p.add_argument("--backend", choices=BACKEND_NAMES,
                   help="data source: nvidia (default), intel, auto (every available backend), "
                        "fake (synthetic, no GPU needed)")
    p.add_argument("--color", choices=("auto", "truecolor", "256", "16"),
                   help="force a colour mode (default: detect from $COLORTERM/$TERM)")
    p.add_argument("--fake-gpus", type=int, default=2, metavar="N", help="number of GPUs for --backend fake")
    p.add_argument("--config", type=Path, metavar="PATH", help="config file to use")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="print detected GPUs and exit")
    mode.add_argument("--once", action="store_true", help="print one snapshot as JSON and exit")
    p.add_argument("--version", action="version", version=f"gputop {__version__}")
    return p


def _err(msg: str) -> None:
    print(f"gputop: error: {msg}", file=sys.stderr)


def _open_backend(name: str, args: argparse.Namespace) -> GPUBackend:
    options = {"count": args.fake_gpus, "seed": None} if name == "fake" else {}
    backend = create_backend(name, **options)
    backend.init()
    return backend


def cmd_list(sampler: Sampler) -> int:
    for name, reason in getattr(sampler.backend, "skipped", {}).items():
        print(f"gputop: note: {name} backend skipped: {reason}", file=sys.stderr)
    devices = sampler.devices
    if not devices:
        print("no GPUs found")
        return 1
    for dev in devices:
        i = dev.info
        parts = [f"GPU {i.index}: {i.name}"]
        if i.short_uuid:
            parts.append(i.short_uuid)
        if i.memory_total:
            parts.append(f"{i.memory_total / 1024**3:.1f} GiB")
        if i.pcie_gen_max:
            parts.append(f"PCIe Gen{i.pcie_gen_max} x{i.pcie_width_max}")
        if i.driver_version:
            parts.append(f"driver {i.driver_version}")
        print(" · ".join(parts))
    return 0


def cmd_once(sampler: Sampler, backend_name: str) -> int:
    sampler.poll_once()
    gpus = []
    for dev in sampler.devices:
        gpus.append(
            {
                "info": dev.info.to_dict(),
                "sample": dev.latest.to_dict() if dev.latest else None,
                "processes": [p.to_dict() for p in dev.processes],
                "lost": dev.lost,
                "error": dev.error,
            }
        )
    out: dict = {"timestamp": time.time(), "backend": backend_name, "gpus": gpus}
    active = getattr(sampler.backend, "active", None)
    if active is not None:  # --backend auto
        out["backends"] = [name for name, _ in active]
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg_path = args.config
    cfg = Config.load(cfg_path)
    if getattr(cfg, "_load_error", None):
        print(f"gputop: warning: {cfg._load_error}", file=sys.stderr)  # type: ignore[attr-defined]

    backend_name = args.backend or cfg.backend
    try:
        backend = _open_backend(backend_name, args)
    except BackendError as exc:
        _err(str(exc))
        return 1

    try:
        interval_ms = args.interval or cfg.interval_ms
        sampler = Sampler(backend, interval=interval_ms / 1000, device_filter=args.gpu)
        try:
            sampler.discover()
        except BackendError as exc:
            _err(str(exc))
            return 1
        if args.gpu is not None:
            found = {d.index for d in sampler.devices}
            missing = sorted(set(args.gpu) - found)
            if missing:
                _err(f"no GPU with index {', '.join(map(str, missing))}")
                return 1
        if not sampler.devices and not args.list:
            _err("no GPUs found")
            return 1

        if args.list:
            return cmd_list(sampler)
        if args.once:
            return cmd_once(sampler, backend_name)
        return run_tui(args, cfg, cfg_path, sampler, backend_name)
    finally:
        backend.shutdown()


def run_tui(args: argparse.Namespace, cfg: Config, cfg_path: Path | None, sampler: Sampler,
            backend_name: str) -> int:
    color = args.color or cfg.color
    if color in COLOR_SYSTEMS:
        os.environ["TEXTUAL_COLOR_SYSTEM"] = COLOR_SYSTEMS[color]

    from .themes import ThemeError, available_themes, get_theme

    cdir = cfg_path.parent if cfg_path else config_dir()
    themes = available_themes(cdir)
    theme_name = args.theme or cfg.theme
    try:
        theme = get_theme(theme_name, cdir)
    except (ThemeError, OSError) as exc:
        if args.theme:
            _err(str(exc))
            return 2
        print(f"gputop: warning: {exc}; using default theme", file=sys.stderr)
        theme = themes["default"]
    themes.setdefault(theme.name, theme)

    # Textual is imported lazily so --list/--once stay fast and dependency-light.
    from .app import GpuTopApp

    label = getattr(sampler.backend, "label", backend_name)
    if sampler.devices and sampler.devices[0].info.driver_version:
        label = f"{backend_name} {sampler.devices[0].info.driver_version.split()[0]}"
    app = GpuTopApp(sampler, themes, theme.name, cfg, backend_label=label)

    def _graceful(signum, frame):  # SIGTERM / SIGHUP (e.g. SSH drop): exit cleanly
        app.exit()

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _graceful)
        except (ValueError, OSError):  # not on the main thread / unsupported
            pass

    try:
        app.run(mouse=not args.no_mouse and cfg.mouse)
    finally:
        sampler.stop()
    try:
        cfg.save_changes(app.changes, cfg_path)
    except OSError as exc:
        print(f"gputop: warning: could not save config: {exc}", file=sys.stderr)
    return app.return_code or 0


if __name__ == "__main__":
    sys.exit(main())
