"""Every GPU on the machine: PCI discovery merged with live vendor backends.

The device list comes from :mod:`gputop.pcidiscovery` (all display
controllers, whatever their state), merged by PCI address with the devices of
the vendor backends that work. GPUs a backend can read *and* that are awake get
live stats; every other GPU is shown with its status only.

Non-waking guarantee: a backend is only ever asked about a GPU after a cheap
PCI status read says it is awake (bound, enabled, not runtime-suspended, not in
D3). That covers backend initialisation too: NVML is not even initialised while
no NVIDIA GPU bound to the ``nvidia`` driver is awake. When every GPU a backend
serves has gone to sleep, the backend is shut down so it doesn't hold the
device open; it is re-initialised when one wakes up.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field, replace

from .. import pcidiscovery as pci
from ..pcidiscovery import PciDiscovery, PciGpu, PciPower
from .base import BackendError, DeviceLostError, GPUBackend, GPUInfo, GPUProcess, GPUSample

log = logging.getLogger(__name__)

# Kernel drivers each backend can read, and the PCI vendor it serves.
BACKEND_DRIVERS: dict[str, frozenset[str]] = {
    "nvidia": frozenset({"nvidia"}),
    "intel": frozenset({"i915", "xe"}),
    "amd": frozenset({"amdgpu"}),
}
FAKE_HINT = "run with --backend fake to try gputop without a GPU"

Factory = Callable[[], GPUBackend]


@dataclass
class _Backend:
    name: str
    factory: Factory
    forced: bool = False
    instance: GPUBackend | None = None
    failed: str | None = None  # permanent failure reason (init error)
    note: str | None = None  # why it was not probed (yet)

    @property
    def ready(self) -> bool:
        return self.instance is not None


@dataclass
class _Slot:
    index: int
    pci: PciGpu | None
    backend: _Backend | None = None  # live binding
    child: GPUInfo | None = None
    power: PciPower | None = None
    was_awake: bool = False
    bind_tried: bool = False  # binding attempted since the GPU last woke up
    reason: str | None = None  # why there are no live stats
    info: GPUInfo = field(default=None)  # type: ignore[assignment]


class MachineBackend(GPUBackend):
    name = "auto"

    def __init__(
        self,
        backends: Sequence[tuple[str, Factory]],
        use_pci: bool = True,
        root: str = "/",
        forced: bool = False,
        discovery: PciDiscovery | None = None,
    ) -> None:
        """
        Args:
            backends: ``(name, factory)`` pairs, e.g. nvidia/intel/amd.
            use_pci: merge in PCI discovery (``--no-pci`` turns it off).
            root: filesystem root (tests use a fake sysfs tree).
            forced: explicit ``--backend``: a backend that fails to initialise
                is an error instead of being skipped.
        """
        self._backends = [_Backend(n, f, forced=forced) for n, f in backends]
        self.use_pci = use_pci
        self.discovery = discovery or PciDiscovery(root)
        self.slots: list[_Slot] = []
        self._initialised = False

    # -- reporting --------------------------------------------------------------

    @property
    def label(self) -> str:
        names = [b.name for b in self._backends if b.ready]
        return "+".join(names) if names else ("pci" if self.use_pci else self.name)

    @property
    def active(self) -> list[tuple[str, GPUBackend]]:
        return [(b.name, b.instance) for b in self._backends if b.instance is not None]

    @property
    def skipped(self) -> dict[str, str]:
        """Backends without a live instance, and why."""
        return {b.name: (b.failed or b.note or "not needed") for b in self._backends if not b.ready}

    # -- lifecycle --------------------------------------------------------------

    def init(self) -> None:
        pci_gpus = self.discovery.discover() if self.use_pci else []
        powers = {g.address: self.discovery.power(g.address) for g in pci_gpus}
        live: list[tuple[_Backend, GPUInfo]] = []
        for b in self._backends:
            drivers = BACKEND_DRIVERS.get(b.name)
            if pci_gpus and drivers is not None:
                bound = [g for g in pci_gpus if powers[g.address] and powers[g.address].driver in drivers]
                awake = [g.address for g in bound if powers[g.address].awake]
                if not awake:
                    driver_list = "/".join(sorted(drivers))
                    b.note = (f"no awake GPU on the {driver_list} driver (not probed, to avoid waking it)"
                              if bound else f"no GPU bound to the {driver_list} driver")
                    if not (b.forced and not bound):
                        continue
                live += self._start(b, awake or None)
            else:
                live += self._start(b, None)
        self._build_slots(pci_gpus, powers, live)
        self._initialised = True
        if not self.slots:
            lines = [f"  {name}: {why}" for name, why in self.skipped.items()]
            raise BackendError("no GPU found\n" + "\n".join(lines + [f"  hint: {FAKE_HINT}"]))

    def _start(self, b: _Backend, addresses: Collection[str] | None) -> list[tuple[_Backend, GPUInfo]]:
        """Initialise backend ``b`` (if needed) and return its devices, limited
        to ``addresses`` (awake GPUs) when PCI information is available."""
        if b.failed:
            return []
        try:
            if b.instance is None:
                inst = b.factory()
                inst.init()
                b.instance, b.note = inst, None
            devices = b.instance.get_devices_for(addresses) if addresses is not None \
                else b.instance.get_devices()
        except BackendError as exc:
            b.failed = str(exc)
            if b.forced:
                raise
            log.info("skipping %s backend: %s", b.name, exc)
            return []
        except Exception as exc:  # a broken backend must not take the others down
            b.failed = f"{type(exc).__name__}: {exc}"
            if b.forced:
                raise BackendError(f"{b.name}: {b.failed}") from exc
            log.exception("%s backend failed", b.name)
            return []
        return [(b, d) for d in devices]

    def _build_slots(self, pci_gpus: list[PciGpu], powers: dict[str, PciPower | None],
                     live: list[tuple[_Backend, GPUInfo]]) -> None:
        old = {s.pci.address: s for s in self.slots if s.pci is not None}
        by_addr = {d.pci_address: (b, d) for b, d in live if d.pci_address}
        slots: list[_Slot] = []
        for g in pci_gpus:
            prev = old.get(g.address)
            slot = _Slot(index=len(slots), pci=g, power=powers.get(g.address))
            if g.address in by_addr:
                slot.backend, slot.child = by_addr.pop(g.address)
            elif prev is not None and prev.backend is not None and prev.backend.ready:
                slot.backend, slot.child = prev.backend, prev.child
            slot.was_awake = bool(slot.power and slot.power.awake)
            slot.bind_tried = True  # init/rediscovery already tried every backend
            if slot.backend is None and slot.was_awake and slot.power is not None:
                slot.reason = self._unavailable_reason(slot.power.driver)
            slots.append(slot)
        # Live devices PCI discovery didn't see (fake backend, --no-pci, containers).
        for b, d in live:
            if d.pci_address and d.pci_address not in by_addr:
                continue  # merged above
            slots.append(_Slot(index=len(slots), pci=None, backend=b, child=d, was_awake=True))
        for s in slots:
            s.info = self._merged_info(s)
        self.slots = slots

    def rediscover(self) -> None:
        """Re-run PCI discovery (SIGUSR1 / Shift+R), keeping live bindings."""
        pci_gpus = self.discovery.discover() if self.use_pci else []
        powers = {g.address: self.discovery.power(g.address) for g in pci_gpus}
        live: list[tuple[_Backend, GPUInfo]] = []
        for s in self.slots:
            if s.pci is None and s.backend is not None and s.child is not None:
                live.append((s.backend, s.child))
        self._build_slots(pci_gpus, powers, live)

    def shutdown(self) -> None:
        for b in self._backends:
            if b.instance is not None:
                try:
                    b.instance.shutdown()
                except Exception:  # noqa: BLE001 - best effort
                    log.exception("shutdown failed")
                b.instance = None
        self._initialised = False

    # -- device list ------------------------------------------------------------

    def _merged_info(self, s: _Slot) -> GPUInfo:
        g, c = s.pci, s.child
        if c is not None:
            info = replace(c, index=s.index)
            if g is not None:
                info = replace(info, pci_address=g.address, vendor=info.vendor or g.vendor,
                               pci_ids=g.ids, boot_vga=g.boot_vga)
            return info
        assert g is not None
        return GPUInfo(index=s.index, name=g.name, uuid=g.address, vendor=g.vendor, pci_address=g.address,
                       pci_ids=g.ids, boot_vga=g.boot_vga)

    def get_devices(self) -> list[GPUInfo]:
        if not self._initialised:
            self.init()
        return [s.info for s in self.slots]

    def refresh_info(self, device: GPUInfo) -> GPUInfo:
        s = self._slot(device)
        return s.info

    def _slot(self, device: GPUInfo) -> _Slot:
        if 0 <= device.index < len(self.slots):
            return self.slots[device.index]
        raise DeviceLostError(device.index)

    # -- status + gating -----------------------------------------------------------

    def _poll(self, s: _Slot) -> PciPower | None:
        """Cheap status read; binds/unbinds backends on wake/sleep transitions."""
        if s.pci is None:
            return None
        p = self.discovery.power(s.pci.address)
        if p is None:
            raise DeviceLostError(s.index, f"GPU {s.index} ({s.pci.address}) is no longer on the PCI bus")
        s.power = p
        awake = p.awake
        if not awake and s.was_awake:
            self._release_if_idle(s)
            s.bind_tried = False
        elif awake and (not s.was_awake or not s.bind_tried):
            self._bind(s, p)
            s.bind_tried = True
        s.was_awake = awake
        return p

    def _unavailable_reason(self, driver: str | None) -> str | None:
        for b in self._backends:
            if driver in BACKEND_DRIVERS.get(b.name, ()):
                return f"{b.name} backend unavailable: {b.failed}" if b.failed else None
        return None

    def _bind(self, s: _Slot, p: PciPower) -> None:
        """A GPU is awake: attach the backend for its driver (initialising it if needed)."""
        if s.backend is not None and s.backend.ready:
            return
        s.backend = s.child = None
        s.reason = None
        for b in self._backends:
            if p.driver in BACKEND_DRIVERS.get(b.name, ()):
                found = self._start(b, [s.pci.address])
                if found:
                    s.backend, s.child = found[0]
                    s.info = self._merged_info(s)
                elif b.failed:
                    s.reason = f"{b.name} backend unavailable: {b.failed}"
                else:
                    s.reason = f"{b.name} backend does not see this GPU"
                break

    def _release_if_idle(self, s: _Slot) -> None:
        """A GPU went to sleep: stop using its backend; shut the backend down
        if none of its GPUs is awake, so it doesn't hold the devices open."""
        b = s.backend
        if b is None or s.pci is None:
            return
        s.backend = s.child = None
        still_used = any(o.backend is b for o in self.slots)
        if not still_used and b.instance is not None:
            try:
                b.instance.shutdown()
            except Exception:  # noqa: BLE001
                log.exception("shutdown of %s failed", b.name)
            b.instance = None
            b.note = "all its GPUs are asleep"

    def status(self, s: _Slot) -> str:
        if s.pci is None:
            return pci.ACTIVE
        p = s.power
        if p is None:
            return pci.OFF
        return pci.classify(p, live=s.backend is not None and s.backend.ready)

    # -- sampling ---------------------------------------------------------------------

    def sample(self, device: GPUInfo) -> GPUSample:
        s = self._slot(device)
        p = self._poll(s)
        status = self.status(s)
        if status == pci.ACTIVE and s.backend is not None and s.backend.instance is not None:
            try:
                out = s.backend.instance.sample(s.child)
            except DeviceLostError:
                # The backend lost it but the PCI device is still there (driver
                # unbound, reset...): fall back to PCI status from now on.
                if s.pci is None:
                    raise DeviceLostError(device.index) from None
                s.backend = s.child = None
                s.reason = "backend lost the device"
                return self._offline_sample(s, p)
            out.index = device.index
            out.status, out.live = pci.ACTIVE, True
            if p is not None:
                out.driver, out.power_state, out.runtime_status = p.driver, p.power_state, p.runtime_status
            s.info = self._merged_info(s)
            return out
        return self._offline_sample(s, p)

    def _offline_sample(self, s: _Slot, p: PciPower | None) -> GPUSample:
        status = self.status(s)
        detail = pci.describe(status, p, s.reason) if p is not None else "not on the PCI bus"
        return GPUSample(
            index=s.index, live=False, status=status, status_detail=detail,
            driver=p.driver if p else None, power_state=p.power_state if p else None,
            runtime_status=p.runtime_status if p else None,
        )

    def get_processes(self, device: GPUInfo) -> list[GPUProcess]:
        s = self._slot(device)
        if self.status(s) != pci.ACTIVE or s.backend is None or s.backend.instance is None:
            return []  # only ACTIVE GPUs are queried
        try:
            procs = s.backend.instance.get_processes(s.child)
        except DeviceLostError:
            return []
        for p in procs:
            p.gpu_index = device.index
        return procs

    def supports_processes(self, device: GPUInfo) -> bool:
        s = self._slot(device)
        if self.status(s) != pci.ACTIVE or s.backend is None or s.backend.instance is None:
            return False
        return s.backend.instance.supports_processes(s.child)
