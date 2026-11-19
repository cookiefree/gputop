"""Compact unit formatting shared by panels, tables and ``--list``."""

from __future__ import annotations

SUPERSCRIPT = "⁰¹²³⁴⁵⁶⁷⁸⁹"


def superscript(n: int) -> str:
    return "".join(SUPERSCRIPT[int(d)] for d in str(n))


def short_gpu_name(name: str) -> str:
    """'NVIDIA GeForce RTX 4090' -> 'RTX 4090'."""
    for prefix in ("NVIDIA GeForce ", "NVIDIA ", "GeForce ", "AMD Radeon ", "Intel(R) ", "Intel "):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def bytes_short(n: float | None, digits: int = 1) -> str:
    """1536 MiB -> '1.5G'. Binary units, single-letter suffix."""
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            if unit in ("B", "K"):
                return f"{n:.0f}{unit}"
            return f"{n:.{digits}f}{unit}"
        n /= 1024
    return f"{n:.{digits}f}T"  # pragma: no cover


def mem_pair(used: int | None, total: int | None) -> str:
    """'9.1/12.0 GB' style, using the total's unit for both numbers."""
    if used is None:
        return "-"
    if not total:
        return bytes_short(used)
    gib = 1024**3
    if total >= gib:
        return f"{used / gib:.1f}/{total / gib:.1f} GB"
    mib = 1024**2
    return f"{used / mib:.0f}/{total / mib:.0f} MB"


def rate(n: float | None) -> str:
    """Bytes/s -> '3.4 GB/s' (decimal units, like PCIe specs)."""
    if n is None:
        return "-"
    for unit, div in (("GB/s", 1e9), ("MB/s", 1e6), ("KB/s", 1e3)):
        if n >= div:
            v = n / div
            return f"{v:.1f} {unit}" if v < 100 else f"{v:.0f} {unit}"
    return f"{n:.0f} B/s"


def pct(v: float | None, width: int = 0) -> str:
    s = "-" if v is None else f"{v:.0f}%"
    return s.rjust(width) if width else s


def watts(v: float | None) -> str:
    return "-" if v is None else f"{v:.0f}W"


def power_pair(draw: float | None, limit: float | None) -> str:
    if draw is None:
        return "-"
    if limit:
        return f"{draw:.0f}/{limit:.0f}W"
    return f"{draw:.0f}W"


def temp(v: float | None) -> str:
    return "-" if v is None else f"{v:.0f}°C"


def mhz(v: float | None) -> str:
    return "-" if v is None else f"{v:.0f}MHz"


def truncate(s: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    if width == 1:
        return "…"
    return s[: width - 1] + "…"
