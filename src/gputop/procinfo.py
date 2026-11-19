"""Resolve PID -> (user, command) via psutil, with a small cache.

Lookups are cached by ``(pid, create_time)`` so a recycled PID is never shown
with a stale command. Processes that exit between sampling and lookup, or
that belong to other users we cannot inspect, degrade to placeholders instead
of raising.
"""

from __future__ import annotations

import threading

import psutil

from .backends.base import GPUProcess


class ProcessResolver:
    def __init__(self) -> None:
        self._cache: dict[int, tuple[float, str, str]] = {}
        self._lock = threading.Lock()

    def resolve(self, procs: list[GPUProcess]) -> list[GPUProcess]:
        """Fill ``user`` and ``command`` in place and return ``procs``."""
        seen: set[int] = set()
        for proc in procs:
            seen.add(proc.pid)
            if proc.user and proc.command:
                continue
            user, command = self._lookup(proc.pid, proc.name_hint)
            if not proc.user:
                proc.user = user
            if not proc.command:
                proc.command = command
        self._prune(seen)
        return procs

    def _lookup(self, pid: int, hint: str) -> tuple[str, str]:
        try:
            p = psutil.Process(pid)
            created = p.create_time()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return "?", hint or f"[{pid}]"
        except (psutil.AccessDenied, OSError):
            return "?", hint or f"[{pid}]"

        with self._lock:
            cached = self._cache.get(pid)
        if cached is not None and cached[0] == created:
            return cached[1], cached[2]

        user = "?"
        command = hint or f"[{pid}]"
        try:
            with p.oneshot():
                try:
                    user = p.username()
                except (psutil.AccessDenied, KeyError):
                    try:
                        user = str(p.uids().real)
                    except psutil.Error:
                        pass
                try:
                    cmdline = p.cmdline()
                except psutil.AccessDenied:
                    cmdline = []
                if cmdline:
                    command = " ".join(cmdline)
                else:
                    try:
                        command = p.name() or command
                    except psutil.Error:
                        pass
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return "?", hint or f"[{pid}]"
        except psutil.Error:
            pass

        with self._lock:
            self._cache[pid] = (created, user, command)
        return user, command

    def _prune(self, alive: set[int]) -> None:
        with self._lock:
            if len(self._cache) > 4 * max(len(alive), 16):
                for pid in [pid for pid in self._cache if pid not in alive]:
                    del self._cache[pid]
