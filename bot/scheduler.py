"""A tiny job scheduler with health tracking (last run, errors, duration) for every background job."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class JobState:
    name: str
    every: float
    description: str = ""
    runs: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_start: float | None = None  # monotonic
    last_ok: float | None = None
    last_error: str | None = None
    last_duration: float = 0.0

    @property
    def healthy(self) -> bool:
        if self.consecutive_failures >= 3:
            return False
        if self.last_ok is None:
            return self.runs == 0 or self.consecutive_failures == 0
        # a job that hasn't succeeded for 3 of its intervals (min 10 min) is stuck
        return time.monotonic() - self.last_ok < max(600.0, self.every * 3 + self.last_duration)

    def ago(self) -> str:
        if self.last_ok is None:
            return "never"
        seconds = int(time.monotonic() - self.last_ok)
        if seconds < 90:
            return f"{seconds}s ago"
        if seconds < 5400:
            return f"{seconds // 60}m ago"
        return f"{seconds // 3600}h ago"


class Scheduler:
    def __init__(self) -> None:
        self.jobs: dict[str, JobState] = {}
        self._specs: list[tuple[JobState, Callable[[], Awaitable[None]], float]] = []
        self._tasks: list[asyncio.Task[None]] = []

    def add(self, name: str, every: float, job: Callable[[], Awaitable[None]], *, first_delay: float = 0, description: str = "") -> None:
        state = JobState(name, every, description)
        self.jobs[name] = state
        self._specs.append((state, job, first_delay))

    async def _loop(self, state: JobState, job: Callable[[], Awaitable[None]], first_delay: float) -> None:
        await asyncio.sleep(first_delay)
        while True:
            state.last_start = time.monotonic()
            state.runs += 1
            try:
                await job()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state.failures += 1
                state.consecutive_failures += 1
                state.last_error = f"{type(exc).__name__}: {exc}"[:200]
                log.exception("job %s failed", state.name)
            else:
                state.last_ok = time.monotonic()
                state.consecutive_failures = 0
            finally:
                state.last_duration = time.monotonic() - (state.last_start or time.monotonic())
            await asyncio.sleep(state.every)

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._loop(s, j, d), name=f"job:{s.name}") for s, j, d in self._specs]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t

    @property
    def unhealthy(self) -> list[JobState]:
        return [s for s in self.jobs.values() if not s.healthy]
