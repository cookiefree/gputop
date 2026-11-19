"""A synthetic multi-GPU backend for development and tests.

Run the full UI without any GPU with ``gputop --backend fake``. Each fake card
follows a different workload profile and deliberately lacks some metrics
(e.g. the laptop GPU has no power limit and no fan) so the "unsupported field"
code paths get exercised too.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .base import DeviceLostError, GPUBackend, GPUInfo, GPUProcess, GPUSample

GiB = 1024**3


@dataclass(frozen=True)
class _Profile:
    name: str
    mem_gib: int
    power_limit: float | None
    pcie: tuple[int, int]
    clocks: tuple[int, int, int]  # graphics, sm, mem max MHz
    workload: str
    has_fan: bool = True
    has_mem_temp: bool = True
    has_codec: bool = True
    has_proc_util: bool = True


PROFILES: tuple[_Profile, ...] = (
    _Profile("NVIDIA GeForce RTX 4090", 24, 450.0, (4, 16), (2790, 2790, 10501), "training"),
    _Profile("NVIDIA GeForce RTX 3060", 12, 170.0, (4, 16), (2100, 2100, 7501), "bursty"),
    _Profile(
        "NVIDIA RTX A2000 Laptop GPU", 4, None, (4, 8), (1740, 1740, 6001), "gaming",
        has_fan=False, has_mem_temp=False, has_codec=False, has_proc_util=False,
    ),
    _Profile("NVIDIA A100-SXM4-80GB", 80, 400.0, (4, 16), (1410, 1410, 1593), "steady", has_fan=False),
)

_COMMANDS = (
    ("python", "python train.py --model llama-8b --bf16"),
    ("ollama", "/usr/local/bin/ollama serve"),
    ("Xorg", "/usr/lib/Xorg :0 -seat seat0 -auth /run/sddm/xauth"),
    ("firefox", "/usr/lib/firefox/firefox -contentproc"),
    ("blender", "blender --background scene.blend --render-anim"),
    ("ffmpeg", "ffmpeg -hwaccel cuda -i input.mkv -c:v h264_nvenc out.mp4"),
    ("kwin_wayland", "/usr/bin/kwin_wayland --wayland-fd 7 --socket wayland-0"),
    ("steam", "/home/user/.steam/ubuntu12_32/steam -srt-logger-opened"),
    ("jupyter", "python -m ipykernel_launcher -f kernel-41a3.json"),
)
_USERS = ("nik", "root", "alice", "ci-runner")


class FakeBackend(GPUBackend):
    name = "fake"

    def __init__(
        self,
        count: int = 2,
        seed: int | None = 1234,
        lose_after: dict[int, int] | None = None,
        dynamic_processes: bool = True,
    ) -> None:
        """
        Args:
            count: number of fake GPUs (profiles repeat after 4).
            seed: RNG seed; ``None`` for non-deterministic output.
            lose_after: ``{gpu_index: n}`` makes that GPU raise
                :class:`DeviceLostError` after ``n`` samples (for testing).
            dynamic_processes: spawn/exit processes over time.
        """
        self.count = max(1, int(count))
        self._rng = random.Random(seed)
        self._lose_after = dict(lose_after or {})
        self._dynamic = dynamic_processes
        self._ticks: dict[int, int] = {}
        self._procs: dict[int, list[GPUProcess]] = {}
        self._next_pid = 4000
        self._devices: list[GPUInfo] = []
        self._initialised = False

    # -- lifecycle ------------------------------------------------------------

    def init(self) -> None:
        self._devices = []
        for i in range(self.count):
            p = PROFILES[i % len(PROFILES)]
            self._devices.append(
                GPUInfo(
                    index=i,
                    name=p.name,
                    uuid=f"GPU-{0x1a2b3c4d + i * 0x1111:08x}-fake-{i:04d}-0000-{i:012d}",
                    driver_version="555.42.02 (fake)",
                    vendor="NVIDIA",
                    memory_total=p.mem_gib * GiB,
                    pcie_gen_max=p.pcie[0],
                    pcie_width_max=p.pcie[1],
                    clock_graphics_max=p.clocks[0],
                    clock_sm_max=p.clocks[1],
                    clock_mem_max=p.clocks[2],
                    power_limit_max=p.power_limit,
                )
            )
            self._ticks[i] = 0
            self._procs[i] = [self._spawn(i) for _ in range(1 + i % 3)]
        self._initialised = True

    def shutdown(self) -> None:
        self._initialised = False

    def get_devices(self) -> list[GPUInfo]:
        if not self._initialised:
            self.init()
        return list(self._devices)

    # -- sampling -------------------------------------------------------------

    def _check_lost(self, index: int) -> None:
        limit = self._lose_after.get(index)
        if limit is not None and self._ticks.get(index, 0) >= limit:
            raise DeviceLostError(index)

    def _load(self, profile: _Profile, t: int) -> float:
        noise = self._rng.uniform(-4, 4)
        if profile.workload == "training":
            # High load with periodic checkpoint dips.
            base = 92.0 if (t % 60) > 6 else 15.0
        elif profile.workload == "bursty":
            base = 8.0 + (75.0 if (t // 15) % 4 == 0 else 0.0) + 6 * math.sin(t / 3)
        elif profile.workload == "gaming":
            base = 62.0 + 30.0 * math.sin(t / 9.0) + 8.0 * math.sin(t / 2.3)
        else:  # steady
            base = 45.0 + 10.0 * math.sin(t / 25.0)
        return max(0.0, min(100.0, base + noise))

    def sample(self, device: GPUInfo) -> GPUSample:
        i = device.index
        self._check_lost(i)
        t = self._ticks[i] = self._ticks.get(i, 0) + 1
        p = PROFILES[i % len(PROFILES)]
        load = self._load(p, t)
        frac = load / 100.0

        mem_total = p.mem_gib * GiB
        procs = self._procs.get(i, [])
        mem_used = int(min(mem_total, 300 * 1024**2 + sum(pr.vram_used or 0 for pr in procs)))

        nominal_power = p.power_limit or 95.0
        power = nominal_power * (0.08 + 0.9 * frac) + self._rng.uniform(-3, 3)
        if p.power_limit:
            power = min(power, p.power_limit)

        temp = 32 + 50 * frac + 3 * math.sin(t / 17)
        g_max, sm_max, m_max = p.clocks
        clock = int(210 + (g_max - 210) * min(1.0, frac * 1.25))

        reasons: list[str] = []
        if load < 5:
            reasons.append("idle")
        if p.power_limit and power >= 0.97 * p.power_limit:
            reasons.append("sw_power")
        if temp > 80:
            reasons.append("sw_thermal")

        codec = p.has_codec and any("ffmpeg" in pr.command for pr in procs)

        return GPUSample(
            index=i,
            util_gpu=round(load, 1),
            util_mem=round(max(0.0, min(100.0, load * 0.6 + self._rng.uniform(-3, 3))), 1),
            mem_used=mem_used,
            mem_total=mem_total,
            temp_core=round(temp, 0),
            temp_mem=round(temp + 8, 0) if p.has_mem_temp else None,
            temp_hotspot=round(temp + 11, 0) if p.has_mem_temp else None,
            fan=round(max(0.0, (temp - 40) * 1.6), 0) if p.has_fan else None,
            power_draw=round(max(5.0, power), 1),
            power_limit=p.power_limit,
            clock_graphics=clock,
            clock_sm=clock,
            clock_mem=m_max if load > 3 else int(m_max * 0.1),
            pcie_tx=max(0.0, frac * 2.4e9 * (0.5 + 0.5 * math.sin(t / 4)) + self._rng.uniform(0, 5e6)),
            pcie_rx=max(0.0, frac * 6.0e9 * (0.5 + 0.5 * math.cos(t / 5)) + self._rng.uniform(0, 5e6)),
            pcie_gen=p.pcie[0] if load > 3 else 1,
            pcie_width=p.pcie[1],
            throttle_reasons=tuple(reasons),
            enc_util=(round(35 + 20 * math.sin(t / 6), 0) if codec else 0.0) if p.has_codec else None,
            dec_util=(round(20 + 10 * math.cos(t / 7), 0) if codec else 0.0) if p.has_codec else None,
        )

    # -- processes ------------------------------------------------------------

    def _spawn(self, gpu: int) -> GPUProcess:
        self._next_pid += self._rng.randint(1, 900)
        name, cmd = self._rng.choice(_COMMANDS)
        ptype = "Graphics" if name in {"Xorg", "kwin_wayland", "firefox", "steam"} else "Compute"
        if name == "blender":
            ptype = "C+G"
        mem_total = PROFILES[gpu % len(PROFILES)].mem_gib * GiB
        return GPUProcess(
            pid=self._next_pid,
            gpu_index=gpu,
            vram_used=int(self._rng.uniform(0.02, 0.25) * mem_total),
            type=ptype,
            user=self._rng.choice(_USERS),
            command=cmd,
            name_hint=name,
        )

    def get_processes(self, device: GPUInfo) -> list[GPUProcess]:
        i = device.index
        self._check_lost(i)
        p = PROFILES[i % len(PROFILES)]
        procs = self._procs.setdefault(i, [])
        t = self._ticks.get(i, 0)
        if self._dynamic and t and t % 20 == 0:
            if len(procs) > 1 and self._rng.random() < 0.5:
                procs.pop(self._rng.randrange(len(procs)))
            elif len(procs) < 8:
                procs.append(self._spawn(i))
        load = self._load(p, t) if t else 0.0
        out = []
        for n, pr in enumerate(procs):
            share = load / (n + 1.5) if p.has_proc_util else None
            out.append(
                GPUProcess(
                    pid=pr.pid,
                    gpu_index=i,
                    vram_used=pr.vram_used,
                    sm_util=None if share is None else round(min(100.0, share), 0),
                    type=pr.type,
                    user=pr.user,
                    command=pr.command,
                    name_hint=pr.name_hint,
                )
            )
        return out
