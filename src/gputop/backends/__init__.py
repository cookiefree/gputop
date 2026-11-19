"""GPU backends, and how the CLI turns ``--backend`` into one backend object."""

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

BACKEND_NAMES = ("auto", "nvidia", "intel", "amd", "fake")
VENDOR_BACKENDS = ("nvidia", "intel", "amd")
# Probed by ``--backend auto`` (each only if an awake GPU uses its driver).
AUTO_BACKENDS = VENDOR_BACKENDS


def create_backend(name: str, **options: Any) -> GPUBackend:
    """Instantiate one vendor backend by name (imports are lazy so missing
    vendor libraries only matter for the backend actually requested)."""
    name = name.lower()
    if name == "nvidia":
        from .nvidia import NvidiaBackend

        return NvidiaBackend()
    if name == "intel":
        from .intel import IntelBackend

        return IntelBackend(**options)
    if name == "fake":
        from .fake import FakeBackend

        return FakeBackend(**options)
    if name == "amd":
        from .amd import AMDBackend

        return AMDBackend()
    if name == "auto":
        return build_backend("auto")
    raise BackendError(f"unknown backend {name!r} (choose from {', '.join(BACKEND_NAMES)})")


def parse_backend_spec(spec: str) -> list[str]:
    """'auto' | 'fake' | 'nvidia' | 'nvidia,intel' -> list of backend names."""
    names = [n.strip().lower() for n in spec.split(",") if n.strip()]
    if not names:
        raise ValueError("empty backend list")
    for n in names:
        if n not in BACKEND_NAMES:
            raise ValueError(f"unknown backend {n!r} (choose from {', '.join(BACKEND_NAMES)})")
    if len(names) > 1 and ("auto" in names or "fake" in names):
        raise ValueError("'auto' and 'fake' can't be combined with other backends")
    return names


def build_backend(spec: str = "auto", use_pci: bool = True, root: str = "/", fake_count: int = 2,
                  fake_seed: int | None = None) -> GPUBackend:
    """The backend the app uses: vendor backends merged with PCI discovery.

    ``auto`` probes every vendor backend and silently skips those that don't
    work; an explicit list forces those backends (a failure is an error).
    PCI-only GPUs (off, suspended, no driver, unsupported driver) are shown
    unless ``use_pci`` is False. ``fake`` never mixes with real PCI GPUs.
    """
    from .machine import MachineBackend

    names = parse_backend_spec(spec)
    if names == ["fake"]:
        return MachineBackend([("fake", lambda: create_backend("fake", count=fake_count, seed=fake_seed))],
                              use_pci=False, forced=True)
    forced = names != ["auto"]
    if not forced:
        names = list(AUTO_BACKENDS)

    def factory(name: str):
        if name == "intel":
            return lambda: create_backend("intel", root=root)
        return lambda: create_backend(name)

    return MachineBackend([(n, factory(n)) for n in names], use_pci=use_pci, root=root, forced=forced)


__all__ = [
    "AUTO_BACKENDS",
    "BACKEND_NAMES",
    "THROTTLE_LABELS",
    "THROTTLE_WARNING",
    "BackendError",
    "DeviceLostError",
    "GPUBackend",
    "GPUInfo",
    "GPUProcess",
    "GPUSample",
    "build_backend",
    "create_backend",
    "parse_backend_spec",
]
