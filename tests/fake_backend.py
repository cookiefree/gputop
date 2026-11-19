"""Fake multi-GPU backends for tests (no hardware required).

``make_fake`` gives the same synthetic backend the CLI's ``--backend fake``
uses, seeded for determinism. ``ScriptedBackend`` returns exactly the values
you hand it, which is what most sampler tests want.
"""

from __future__ import annotations

from collections.abc import Callable

from gputop.backends.base import DeviceLostError, GPUBackend, GPUInfo, GPUProcess, GPUSample
from gputop.backends.fake import FakeBackend


def make_fake(count: int = 3, **kwargs) -> FakeBackend:
    kwargs.setdefault("seed", 42)
    backend = FakeBackend(count=count, **kwargs)
    backend.init()
    return backend


class ScriptedBackend(GPUBackend):
    """Backend whose samples come from a function ``f(index, tick) -> util``.

    ``lose`` maps GPU index -> tick at which it raises DeviceLostError.
    ``fail`` maps GPU index -> tick at which it raises a generic error once.
    """

    name = "scripted"

    def __init__(
        self,
        count: int = 2,
        util: Callable[[int, int], float | None] = lambda i, t: float(t),
        lose: dict[int, int] | None = None,
        fail: dict[int, int] | None = None,
        processes: dict[int, list[GPUProcess]] | None = None,
    ) -> None:
        self.count = count
        self.util = util
        self.lose = lose or {}
        self.fail = fail or {}
        self.procs = processes or {}
        self.ticks = {i: 0 for i in range(count)}
        self.shutdown_called = False

    def get_devices(self) -> list[GPUInfo]:
        return [
            GPUInfo(index=i, name=f"Scripted GPU {i}", uuid=f"GPU-{i:08x}-0000", memory_total=8 << 30)
            for i in range(self.count)
        ]

    def sample(self, device: GPUInfo) -> GPUSample:
        i = device.index
        self.ticks[i] += 1
        t = self.ticks[i]
        if self.lose.get(i) is not None and t >= self.lose[i]:
            raise DeviceLostError(i)
        if self.fail.get(i) == t:
            raise RuntimeError("transient driver hiccup")
        return GPUSample(index=i, util_gpu=self.util(i, t), mem_used=t << 20, mem_total=8 << 30)

    def get_processes(self, device: GPUInfo) -> list[GPUProcess]:
        return [
            GPUProcess(pid=p.pid, gpu_index=p.gpu_index, vram_used=p.vram_used, user=p.user,
                       command=p.command, type=p.type)
            for p in self.procs.get(device.index, [])
        ]

    def shutdown(self) -> None:
        self.shutdown_called = True
