"""GPU backends and a tiny registry so the CLI can pick one by name."""

from __future__ import annotations

from typing import Any

from .base import (
    THROTTLE_LABELS,
    THROTTLE_WARNING,
    BackendError,
    DeviceLostError,
    GPUBackend,
    GPUInfo,
    GPUProcess,
    GPUSample,
)

BACKEND_NAMES = ("nvidia", "fake", "amd")


def create_backend(name: str, **options: Any) -> GPUBackend:
    """Instantiate a backend by name (imports are lazy so missing vendor
    libraries only matter for the backend actually requested)."""
    name = name.lower()
    if name == "nvidia":
        from .nvidia import NvidiaBackend

        return NvidiaBackend()
    if name == "fake":
        from .fake import FakeBackend

        return FakeBackend(**options)
    if name == "amd":
        from .amd import AMDBackend

        return AMDBackend()
    raise BackendError(f"unknown backend {name!r} (choose from {', '.join(BACKEND_NAMES)})")


__all__ = [
    "BACKEND_NAMES",
    "THROTTLE_LABELS",
    "THROTTLE_WARNING",
    "BackendError",
    "DeviceLostError",
    "GPUBackend",
    "GPUInfo",
    "GPUProcess",
    "GPUSample",
    "create_backend",
]
