"""Background polling of a :class:`GPUBackend` plus per-metric ring buffers.

The sampler owns a worker thread that calls the backend at a fixed interval,
so slow vendor calls (NVML's PCIe throughput query alone blocks ~20 ms) never
stall rendering. The UI reads the latest values and history through
thread-safe accessors and is notified through an ``on_update`` callback.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

from .backends.base import DeviceLostError, GPUBackend, GPUInfo, GPUProcess, GPUSample
from .procinfo import ProcessResolver

log = logging.getLogger(__name__)

# Metrics kept in history. Names are GPUSample attributes or properties.
HISTORY_METRICS: tuple[str, ...] = (
    "util_gpu",
    "util_mem",
    "mem_used",
    "mem_percent",
    "temp_core",
    "temp_mem",
    "temp_hotspot",
    "fan",
    "power_draw",
    "power_percent",
    "clock_graphics",
    "clock_sm",
    "clock_mem",
    "pcie_tx",
    "pcie_rx",
    "enc_util",
    "dec_util",
)

MIN_INTERVAL = 0.1
MAX_INTERVAL = 5.0
DEFAULT_HISTORY = 1024


class RingBuffer:
    """Fixed-capacity FIFO of floats (``None`` = no data for that tick).

    Appending past capacity drops the oldest value. All methods are safe to
    call from multiple threads.
    """

    __slots__ = ("_data", "_lock")

    def __init__(self, capacity: int, lock: threading.RLock | None = None) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._data: deque[float | None] = deque(maxlen=capacity)
        self._lock = lock or threading.RLock()

    @property
    def capacity(self) -> int:
        assert self._data.maxlen is not None
        return self._data.maxlen

    def append(self, value: float | None) -> None:
        with self._lock:
            self._data.append(value)

    def extend(self, values: Iterable[float | None]) -> None:
        with self._lock:
            self._data.extend(values)

    def tail(self, n: int | None = None) -> list[float | None]:
        """Return the newest ``n`` values (oldest first). ``None`` -> all."""
        with self._lock:
            if n is None or n >= len(self._data):
                return list(self._data)
            if n <= 0:
                return []
            # deque has no slicing; iterate from the right and reverse.
            out = []
            it = reversed(self._data)
            for _ in range(n):
                out.append(next(it))
            out.reverse()
            return out

    def values(self) -> list[float | None]:
        return self.tail(None)

    @property
    def latest(self) -> float | None:
        with self._lock:
            return self._data[-1] if self._data else None

    def max(self, n: int | None = None) -> float | None:
        vals = [v for v in self.tail(n) if v is not None]
        return max(vals) if vals else None

    def resize(self, capacity: int) -> None:
        """Change capacity, keeping the newest values."""
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        with self._lock:
            if capacity != self._data.maxlen:
                self._data = deque(self._data, maxlen=capacity)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __iter__(self) -> Iterator[float | None]:
        return iter(self.values())

    def __repr__(self) -> str:
        return f"RingBuffer(capacity={self.capacity}, len={len(self)})"


@dataclass
class DeviceState:
    """Everything the UI needs to know about one GPU."""

    info: GPUInfo
    history: dict[str, RingBuffer]
    latest: GPUSample | None = None
    processes: list[GPUProcess] = field(default_factory=list)
    lost: bool = False
    error: str | None = None

    @property
    def index(self) -> int:
        return self.info.index


class Sampler:
    def __init__(
        self,
        backend: GPUBackend,
        interval: float = 0.5,
        history: int = DEFAULT_HISTORY,
        device_filter: Iterable[int] | None = None,
        on_update: Callable[[int], None] | None = None,
        resolve_processes: bool = True,
    ) -> None:
        self.backend = backend
        self._interval = self._clamp(interval)
        self._history_len = max(1, int(history))
        self._filter = None if device_filter is None else set(device_filter)
        self.on_update = on_update
        self.on_rediscover: Callable[[bool], None] | None = None
        self._rediscover_requested = False
        self._resolver = ProcessResolver() if resolve_processes else None
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._devices: list[DeviceState] = []
        self.generation = 0
        self.last_poll_duration = 0.0

    # -- setup ----------------------------------------------------------------

    @staticmethod
    def _clamp(interval: float) -> float:
        return min(MAX_INTERVAL, max(MIN_INTERVAL, float(interval)))

    def discover(self) -> list[DeviceState]:
        """Query the backend for devices (applying the index filter)."""
        devices = self.backend.get_devices()
        if self._filter is not None:
            devices = [d for d in devices if d.index in self._filter]
        with self._lock:
            self._devices = [
                DeviceState(
                    info=d,
                    history={
                        m: RingBuffer(self._history_len, self._lock) for m in HISTORY_METRICS
                    },
                )
                for d in devices
            ]
            return list(self._devices)

    def rediscover(self) -> bool:
        """Re-run device discovery (e.g. on SIGUSR1). Keeps the history of
        GPUs that are still present (matched by PCI address or UUID).
        Returns True if the device list changed."""
        hook = getattr(self.backend, "rediscover", None)
        if hook is not None:
            hook()
        old = {self._key(d.info): d for d in self.devices}
        before = [self._key(d.info) for d in self.devices]
        devices = self.backend.get_devices()
        if self._filter is not None:
            devices = [d for d in devices if d.index in self._filter]
        with self._lock:
            states = []
            for info in devices:
                prev = old.get(self._key(info))
                if prev is not None:
                    prev.info = info
                    states.append(prev)
                else:
                    states.append(DeviceState(info=info, history={
                        m: RingBuffer(self._history_len, self._lock) for m in HISTORY_METRICS}))
            self._devices = states
        return [self._key(d) for d in devices] != before

    @staticmethod
    def _key(info: GPUInfo) -> str:
        return info.pci_address or info.uuid or f"{info.name}#{info.index}"

    @property
    def devices(self) -> list[DeviceState]:
        with self._lock:
            return list(self._devices)

    def device(self, index: int) -> DeviceState | None:
        with self._lock:
            for dev in self._devices:
                if dev.index == index:
                    return dev
        return None

    @property
    def interval(self) -> float:
        return self._interval

    @interval.setter
    def interval(self, value: float) -> None:
        self._interval = self._clamp(value)
        self._wake.set()  # re-schedule immediately

    @property
    def history_len(self) -> int:
        return self._history_len

    def set_history_len(self, n: int) -> None:
        n = max(1, int(n))
        with self._lock:
            self._history_len = n
            for dev in self._devices:
                for buf in dev.history.values():
                    buf.resize(n)

    # -- polling --------------------------------------------------------------

    def poll_once(self) -> None:
        """Sample every (non-lost) device once. Never raises."""
        started = time.monotonic()
        for dev in self.devices:
            if dev.lost:
                continue
            try:
                sample = self.backend.sample(dev.info)
                procs = self.backend.get_processes(dev.info)
            except DeviceLostError as exc:
                with self._lock:
                    dev.lost = True
                    dev.error = str(exc)
                    dev.processes = []
                log.warning("GPU %s lost: %s", dev.index, exc)
                continue
            except Exception as exc:  # backend bug: keep running, show error
                log.exception("sampling GPU %s failed", dev.index)
                with self._lock:
                    dev.error = f"{type(exc).__name__}: {exc}"
                continue

            if self._resolver is not None:
                try:
                    self._resolver.resolve(procs)
                except Exception:  # psutil oddities must not kill sampling
                    log.exception("process resolution failed")

            try:
                info = self.backend.refresh_info(dev.info)
            except Exception:  # noqa: BLE001 - keep the old info
                info = dev.info

            with self._lock:
                dev.info = info
                dev.latest = sample
                dev.processes = procs
                dev.error = None
                # A GPU that is off/suspended/unreadable isn't queried: its
                # history freezes and resumes when it wakes up.
                if sample.live:
                    for metric, buf in dev.history.items():
                        value = getattr(sample, metric, None)
                        buf.append(None if value is None else float(value))
        with self._lock:
            self.generation += 1
            gen = self.generation
        self.last_poll_duration = time.monotonic() - started
        if self.on_update is not None:
            try:
                self.on_update(gen)
            except Exception:
                log.exception("on_update callback failed")

    def request_rediscover(self) -> None:
        """Ask the sampler thread to re-run discovery before its next poll
        (so it never races with sampling). Thread-safe."""
        self._rediscover_requested = True
        self._wake.set()

    def _run(self) -> None:
        while not self._stopping:
            started = time.monotonic()
            if self._rediscover_requested:
                self._rediscover_requested = False
                try:
                    changed = self.rediscover()
                except Exception:  # discovery problems must not stop sampling
                    log.exception("rediscovery failed")
                    changed = False
                if self.on_rediscover is not None:
                    self.on_rediscover(changed)
            self.poll_once()
            if self._stopping:
                break
            remaining = self._interval - (time.monotonic() - started)
            if remaining > 0:
                self._wake.wait(remaining)
            self._wake.clear()

    def start(self) -> None:
        """Discover devices if needed and start the background thread."""
        if self._thread is not None:
            return
        if not self._devices:
            self.discover()
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="gputop-sampler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stopping = True
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- convenience for UI ---------------------------------------------------

    def all_processes(self) -> list[GPUProcess]:
        with self._lock:
            out: list[GPUProcess] = []
            for dev in self._devices:
                out.extend(dev.processes)
            return out
