"""Run several backends side by side (``--backend auto``).

Each child backend keeps its own device numbering; the composite renumbers
devices 0..N-1 across all children (in the order the backends are listed) and
maps calls back to the right child. Backends that fail to initialise or find
no devices are skipped; only if *none* work is that an error.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace

from .base import BackendError, DeviceLostError, GPUBackend, GPUInfo, GPUProcess, GPUSample

log = logging.getLogger(__name__)


class CompositeBackend(GPUBackend):
    name = "auto"

    def __init__(self, factories: list[tuple[str, Callable[[], GPUBackend]]]) -> None:
        """``factories``: ``(name, create)`` pairs, created lazily in init()."""
        self._factories = factories
        self.active: list[tuple[str, GPUBackend]] = []
        self.skipped: dict[str, str] = {}  # backend name -> reason
        self._map: dict[int, tuple[GPUBackend, GPUInfo]] = {}
        self._devices: list[GPUInfo] = []
        self._initialised = False

    @property
    def label(self) -> str:
        return "+".join(name for name, _ in self.active) or self.name

    def init(self) -> None:
        self.active, self.skipped = [], {}
        self._map, self._devices = {}, []
        for name, factory in self._factories:
            try:
                backend = factory()
                backend.init()
                devices = backend.get_devices()
            except BackendError as exc:
                self.skipped[name] = str(exc)
                log.info("auto: skipping %s backend: %s", name, exc)
                continue
            except Exception as exc:  # a broken backend must not stop the others
                self.skipped[name] = f"{type(exc).__name__}: {exc}"
                log.exception("auto: %s backend failed", name)
                continue
            if not devices:
                self.skipped[name] = "no devices"
                backend.shutdown()
                continue
            self.active.append((name, backend))
            for dev in devices:
                g = len(self._devices)
                info = replace(dev, index=g)
                self._devices.append(info)
                self._map[g] = (backend, dev)
        self._initialised = True
        if not self.active:
            reasons = "; ".join(f"{n}: {r}" for n, r in self.skipped.items()) or "no backends configured"
            raise BackendError(f"no usable GPU backend found ({reasons})")

    def shutdown(self) -> None:
        for _, backend in self.active:
            try:
                backend.shutdown()
            except Exception:  # noqa: BLE001 - best effort
                log.exception("shutdown failed")
        self._initialised = False

    def get_devices(self) -> list[GPUInfo]:
        if not self._initialised:
            self.init()
        return list(self._devices)

    def _child(self, device: GPUInfo) -> tuple[GPUBackend, GPUInfo]:
        try:
            return self._map[device.index]
        except KeyError:
            raise DeviceLostError(device.index) from None

    def sample(self, device: GPUInfo) -> GPUSample:
        backend, child = self._child(device)
        try:
            s = backend.sample(child)
        except DeviceLostError as exc:
            raise DeviceLostError(device.index, str(exc)) from exc
        s.index = device.index
        return s

    def get_processes(self, device: GPUInfo) -> list[GPUProcess]:
        backend, child = self._child(device)
        try:
            procs = backend.get_processes(child)
        except DeviceLostError as exc:
            raise DeviceLostError(device.index, str(exc)) from exc
        for p in procs:
            p.gpu_index = device.index
        return procs

    def supports_processes(self, device: GPUInfo) -> bool:
        backend, child = self._child(device)
        return backend.supports_processes(child)
