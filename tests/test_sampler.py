import os
import threading
import time

import pytest

from fake_backend import ScriptedBackend, make_fake
from gputop.backends.base import GPUProcess
from gputop.sampler import HISTORY_METRICS, MAX_INTERVAL, MIN_INTERVAL, RingBuffer, Sampler

# -- RingBuffer --------------------------------------------------------------


def test_ring_buffer_keeps_newest_values():
    rb = RingBuffer(3)
    for v in range(5):
        rb.append(v)
    assert rb.values() == [2, 3, 4]
    assert len(rb) == 3
    assert rb.latest == 4


def test_ring_buffer_tail():
    rb = RingBuffer(10)
    rb.extend([1, 2, 3, 4, 5])
    assert rb.tail(2) == [4, 5]
    assert rb.tail(0) == []
    assert rb.tail(50) == [1, 2, 3, 4, 5]
    assert rb.tail() == [1, 2, 3, 4, 5]


def test_ring_buffer_empty():
    rb = RingBuffer(4)
    assert rb.values() == []
    assert rb.latest is None
    assert rb.max() is None
    assert len(rb) == 0


def test_ring_buffer_none_values_and_max():
    rb = RingBuffer(5)
    rb.extend([1.0, None, 7.0, None])
    assert rb.values() == [1.0, None, 7.0, None]
    assert rb.max() == 7.0
    assert rb.max(1) is None


def test_ring_buffer_resize_keeps_newest():
    rb = RingBuffer(5)
    rb.extend(range(5))
    rb.resize(3)
    assert rb.values() == [2, 3, 4]
    assert rb.capacity == 3
    rb.resize(6)
    rb.append(9)
    assert rb.values() == [2, 3, 4, 9]


def test_ring_buffer_rejects_bad_capacity():
    with pytest.raises(ValueError):
        RingBuffer(0)
    rb = RingBuffer(2)
    with pytest.raises(ValueError):
        rb.resize(0)


def test_ring_buffer_concurrent_append_and_read():
    rb = RingBuffer(64)
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            rb.append(i)
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(2000):
            vals = rb.tail(32)
            assert len(vals) <= 32
            assert vals == sorted(vals)
    finally:
        stop.set()
        t.join()


# -- Sampler -----------------------------------------------------------------


def test_sampler_fills_history_per_device():
    s = Sampler(ScriptedBackend(count=2), history=4, resolve_processes=False)
    s.discover()
    for _ in range(6):
        s.poll_once()
    for dev in s.devices:
        assert set(dev.history) == set(HISTORY_METRICS)
        assert dev.history["util_gpu"].values() == [3.0, 4.0, 5.0, 6.0]
        assert dev.latest is not None and dev.latest.util_gpu == 6.0
        # mem_percent is a derived property and must be recorded too
        assert dev.history["mem_percent"].latest == pytest.approx(100 * (6 << 20) / (8 << 30))
        # unsupported metrics are stored as None, not 0
        assert dev.history["power_draw"].values() == [None] * 4
    assert s.generation == 6


def test_sampler_device_filter():
    s = Sampler(ScriptedBackend(count=4), device_filter=[1, 3], resolve_processes=False)
    assert [d.index for d in s.discover()] == [1, 3]


def test_sampler_marks_lost_device_and_keeps_others():
    backend = ScriptedBackend(count=2, lose={1: 3})
    s = Sampler(backend, resolve_processes=False)
    s.discover()
    for _ in range(5):
        s.poll_once()
    gpu0, gpu1 = s.devices
    assert not gpu0.lost and gpu0.latest.util_gpu == 5.0
    assert gpu1.lost and gpu1.error
    # lost device is not polled again
    assert backend.ticks[1] == 3
    assert len(gpu1.history["util_gpu"]) == 2


def test_sampler_survives_backend_exception():
    s = Sampler(ScriptedBackend(count=1, fail={0: 2}), resolve_processes=False)
    s.discover()
    s.poll_once()
    s.poll_once()
    dev = s.devices[0]
    assert dev.error and "hiccup" in dev.error
    assert not dev.lost
    s.poll_once()
    assert dev.error is None
    assert dev.history["util_gpu"].values() == [1.0, 3.0]


def test_sampler_resolves_processes_that_exited():
    # A PID that cannot exist on Linux (> pid_max) simulates a process that
    # exited between NVML reporting it and the psutil lookup.
    gone = GPUProcess(pid=2**31 - 7, gpu_index=0, vram_used=1 << 20, name_hint="ghost")
    me = GPUProcess(pid=os.getpid(), gpu_index=0, vram_used=2 << 20)
    backend = ScriptedBackend(count=1, processes={0: [gone, me]})
    s = Sampler(backend)
    s.discover()
    s.poll_once()
    procs = {p.pid: p for p in s.devices[0].processes}
    assert procs[gone.pid].user == "?"
    assert procs[gone.pid].command == f"[{gone.pid}]"
    assert procs[os.getpid()].user not in ("", "?")
    assert "python" in procs[os.getpid()].command.lower() or "pytest" in procs[os.getpid()].command


def test_sampler_interval_is_clamped():
    s = Sampler(ScriptedBackend(), interval=0.001)
    assert s.interval == MIN_INTERVAL
    s.interval = 999
    assert s.interval == MAX_INTERVAL


def test_sampler_background_thread_calls_back():
    updates = []
    done = threading.Event()

    def on_update(gen):
        updates.append(gen)
        if len(updates) >= 3:
            done.set()

    s = Sampler(ScriptedBackend(count=2), interval=0.1, on_update=on_update, resolve_processes=False)
    s.start()
    try:
        assert done.wait(3.0), "sampler thread did not poll"
        assert s.running
    finally:
        s.stop()
    assert not s.running
    assert updates[:3] == [1, 2, 3]


def test_sampler_stop_is_prompt_with_long_interval():
    s = Sampler(ScriptedBackend(), interval=MAX_INTERVAL, resolve_processes=False)
    s.start()
    time.sleep(0.05)
    t0 = time.monotonic()
    s.stop()
    assert time.monotonic() - t0 < 1.0


def test_set_history_len_resizes_all_buffers():
    s = Sampler(ScriptedBackend(), history=10, resolve_processes=False)
    s.discover()
    for _ in range(10):
        s.poll_once()
    s.set_history_len(3)
    assert s.devices[0].history["util_gpu"].values() == [8.0, 9.0, 10.0]


def test_sampler_with_fake_backend_produces_sane_values():
    s = Sampler(make_fake(count=4), resolve_processes=False)
    s.discover()
    for _ in range(30):
        s.poll_once()
    for dev in s.devices:
        smp = dev.latest
        assert 0 <= smp.util_gpu <= 100
        assert 0 < smp.mem_used <= smp.mem_total
        assert dev.processes, "every fake GPU should have processes"
    # The fake laptop GPU (index 2) has no power limit and no fan.
    laptop = s.devices[2].latest
    assert laptop.power_limit is None and laptop.fan is None
    assert s.devices[2].history["power_percent"].latest is None


def test_fake_backend_device_loss():
    s = Sampler(make_fake(count=2, lose_after={0: 5}), resolve_processes=False)
    s.discover()
    for _ in range(8):
        s.poll_once()
    assert s.devices[0].lost
    assert not s.devices[1].lost
