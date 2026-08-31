"""Controllable timeline test doubles shared by timeline test suites."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from backend.timeline.cycle_runner import ChannelCycleRunner
from backend.timeline.models import (
    SCHEMA_VERSION,
    CycleGapPolicy,
    CycleRecord,
    ReplayManifest,
    SessionStatus,
    Timeline,
)


@dataclass
class _SleepWaiter:
    deadline_ms: int
    future: asyncio.Future[None]


class ControlledSleeper:
    """A monotonic clock plus explicitly advanced interruptible sleeps."""

    def __init__(self) -> None:
        self.now_ms = 0
        self.sleep_calls: list[int] = []
        self.cancellations = 0
        self._waiters: list[_SleepWaiter] = []

    async def sleep(self, ms: int) -> None:
        if ms <= 0:
            raise AssertionError("controlled sleeps must have a positive duration")
        self.sleep_calls.append(ms)
        future = asyncio.get_running_loop().create_future()
        waiter = _SleepWaiter(self.now_ms + ms, future)
        self._waiters.append(waiter)
        try:
            await future
        except asyncio.CancelledError:
            self.cancellations += 1
            raise
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    def advance(self, ms: int) -> None:
        if ms < 0:
            raise ValueError("advance duration must be non-negative")
        self.now_ms += ms
        for waiter in tuple(self._waiters):
            if waiter.deadline_ms <= self.now_ms and not waiter.future.done():
                waiter.future.set_result(None)

    @property
    def next_remaining_ms(self) -> int | None:
        pending = [
            waiter.deadline_ms - self.now_ms
            for waiter in self._waiters
            if not waiter.future.done()
        ]
        return min(pending) if pending else None


class SequenceGapRandom:
    """Drive CycleGapPolicy through a literal sequence of gap tenths."""

    def __init__(self, gap_tenths: Sequence[int]) -> None:
        self._values = list(gap_tenths)
        self.calls = 0
        self._pending_randint: int | None = None

    def randrange(self, stop: int) -> int:
        if stop != 100:
            raise AssertionError(f"unexpected randrange stop: {stop}")
        self.calls += 1
        value = self._values.pop(0) if self._values else 0
        if value == 0:
            self._pending_randint = None
            return 0
        self._pending_randint = value
        return 40 if value <= 10 else 70

    def randint(self, start: int, stop: int) -> int:
        value = self._pending_randint
        self._pending_randint = None
        if value is None or not start <= value <= stop:
            raise AssertionError(f"unexpected randint band {start}..{stop} for {value}")
        return value


class DeferredRunnerStart:
    """Task factory that holds ChannelCycleRunner._run before its first step."""

    def __init__(self) -> None:
        self.created = asyncio.Event()
        self.release = asyncio.Event()

    def __call__(
        self,
        loop: asyncio.AbstractEventLoop,
        coro: Any,
        context: Any = None,
    ) -> asyncio.Task[Any]:
        if getattr(getattr(coro, "cr_code", None), "co_name", None) == "_run":
            original_coro = coro

            async def deferred() -> Any:
                self.created.set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    original_coro.close()
                    raise
                return await original_coro

            coro = deferred()
        return asyncio.Task(coro, loop=loop, context=context)


class BlockingCycleCallback:
    """Expose deterministic entry/release points for an async record callback."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.records: list[CycleRecord] = []

    async def __call__(self, record: CycleRecord) -> None:
        self.records.append(record)
        self.entered.set()
        await self.release.wait()


class DurableCycleRecorder:
    """Idempotent persistence fake that can fail before writing a record."""

    def __init__(self, failure: str) -> None:
        self.failure = failure
        self.attempts: list[tuple[str, int]] = []
        self.records: dict[tuple[str, int], CycleRecord] = {}

    async def __call__(self, record: CycleRecord) -> None:
        key = (record.channel, record.cycle_index)
        self.attempts.append(key)
        if self.failure == "cancel":
            raise asyncio.CancelledError
        if self.failure == "raise":
            raise RuntimeError("persistence unavailable")
        self.records[key] = record


class FakeCycleExecutor:
    """Resolve high-level actions without touching a relay or safety state."""

    def __init__(
        self,
        *,
        frames: Mapping[str, Sequence[str]],
        fail_on_cycle: int | None = None,
        disconnect_on_cycle: int | None = None,
        raise_on_cycle: int | None = None,
        effective_strength: int | None = None,
        omit_effective_strength: bool = False,
        invalid_effective_strength: bool = False,
        block_clear: bool = False,
    ) -> None:
        self.frames = {name: tuple(values) for name, values in frames.items()}
        self.fail_on_cycle = fail_on_cycle
        self.disconnect_on_cycle = disconnect_on_cycle
        self.raise_on_cycle = raise_on_cycle
        self.effective_strength = effective_strength
        self.omit_effective_strength = omit_effective_strength
        self.invalid_effective_strength = invalid_effective_strength
        self.block_clear = block_clear
        self.clear_started = asyncio.Event()
        self.release_clear = asyncio.Event()
        self.sent_cycles: list[tuple[str, str, int]] = []
        self.sent_patterns: list[str] = []
        self.strength_calls: list[tuple[str, int]] = []
        self.clear_calls: list[str | None] = []
        self.execute_calls: list[list[dict[str, Any]]] = []
        self._cycle_attempts = 0
        self._strengths = {"A": 0, "B": 0}

    async def execute(
        self, actions: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        self.execute_calls.append([dict(action) for action in actions])
        executed: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        for action in actions:
            op = action.get("op")
            channel = action.get("channel")
            if op == "hold_strength":
                value = action["value"]
                effective_value = (
                    value if self.effective_strength is None else self.effective_strength
                )
                self._strengths[channel] = effective_value
                self.strength_calls.append((channel, value))
                effective = {
                    "op": op,
                    "channel": channel,
                    "requested_strength": value,
                }
                if not self.omit_effective_strength:
                    effective["effective_strength"] = (
                        "invalid" if self.invalid_effective_strength else effective_value
                    )
                executed.append({"action": action, "effective": effective})
                continue
            if op == "clear":
                self.clear_calls.append(channel)
                self.clear_started.set()
                if self.block_clear:
                    await self.release_clear.wait()
                self._strengths[channel] = 0
                executed.append({"action": action, "effective": dict(action)})
                continue
            if op != "pulse_cycle":
                dropped.append({"action": action, "reason": "unsupported action"})
                continue

            self._cycle_attempts += 1
            if self.raise_on_cycle == self._cycle_attempts:
                raise RuntimeError("injected executor failure")
            if self.fail_on_cycle == self._cycle_attempts:
                dropped.append({"action": action, "reason": "injected rejection"})
                continue
            if self.disconnect_on_cycle == self._cycle_attempts:
                dropped.append({"action": action, "reason": "device disconnected"})
                continue

            pattern = action["pattern"]
            frame_count = len(self.frames[pattern])
            self.sent_cycles.append((channel, pattern, frame_count))
            self.sent_patterns.append(pattern)
            executed.append(
                {
                    "action": action,
                    "effective": {
                        "op": op,
                        "channel": channel,
                        "pattern": pattern,
                        "effective_strength": self._strengths[channel],
                        "duration_ms": frame_count * 100,
                    },
                }
            )
        return executed, dropped


class CycleHarness:
    """Wire one runner to controllable time and an in-memory executor."""

    def __init__(
        self,
        *,
        frames: Mapping[str, Sequence[str]],
        gap_tenths: Sequence[int] = (),
        channel: str = "A",
        rng: random.Random | SequenceGapRandom | None = None,
        fail_on_cycle: int | None = None,
        disconnect_on_cycle: int | None = None,
        raise_on_cycle: int | None = None,
        effective_strength: int | None = None,
        omit_effective_strength: bool = False,
        invalid_effective_strength: bool = False,
        block_clear: bool = False,
        on_cycle: Any = None,
    ) -> None:
        self.sleeper = ControlledSleeper()
        self.rng = rng if rng is not None else SequenceGapRandom(gap_tenths)
        self.executor = FakeCycleExecutor(
            frames=frames,
            fail_on_cycle=fail_on_cycle,
            disconnect_on_cycle=disconnect_on_cycle,
            raise_on_cycle=raise_on_cycle,
            effective_strength=effective_strength,
            omit_effective_strength=omit_effective_strength,
            invalid_effective_strength=invalid_effective_strength,
            block_clear=block_clear,
        )
        self.records: list[CycleRecord] = []
        record_callback = self.records.append if on_cycle is None else on_cycle

        self.runner = ChannelCycleRunner(
            channel=channel,
            policy=CycleGapPolicy(),
            rng=self.rng,
            frames=frames,
            executor=self.executor,
            clock=lambda: self.sleeper.now_ms / 1000,
            sleeper=self.sleeper,
            on_cycle=record_callback,
        )

    @property
    def sent_cycles(self) -> list[tuple[str, str, int]]:
        return self.executor.sent_cycles

    @property
    def sent_patterns(self) -> list[str]:
        return self.executor.sent_patterns

    @property
    def clear_calls(self) -> list[str | None]:
        return self.executor.clear_calls

    async def flush(self, turns: int = 8) -> None:
        for _ in range(turns):
            await asyncio.sleep(0)

    async def enter_gap(self) -> None:
        from backend.timeline.cycle_runner import RunnerPhase

        for _ in range(20):
            if self.runner.state().phase is RunnerPhase.GAP:
                return
            remaining = self.sleeper.next_remaining_ms
            if remaining is not None:
                self.sleeper.advance(remaining)
            await asyncio.sleep(0)
        raise AssertionError(f"runner did not enter a gap: {self.runner.state()!r}")

    async def complete_cycle(self) -> CycleRecord:
        from backend.timeline.cycle_runner import RunnerPhase

        initial_records = len(self.records)
        for _ in range(40):
            if len(self.records) > initial_records:
                return self.records[-1]
            phase = self.runner.state().phase
            remaining = self.sleeper.next_remaining_ms
            if remaining is not None and phase in (RunnerPhase.CYCLE, RunnerPhase.GAP):
                self.sleeper.advance(remaining)
            await asyncio.sleep(0)
        raise AssertionError(f"cycle did not complete: {self.runner.state()!r}")

    async def close(self) -> None:
        await self.runner.stop(clear=False, reason="test_cleanup")


def make_replay_bundle(gap_tenths: Sequence[int], status: str) -> SimpleNamespace:
    """Build a complete model-compatible archive fixture for later suites."""

    session_status = SessionStatus(status)
    cycles = tuple(
        CycleRecord(
            channel="A",
            cycle_index=index,
            plot_event_id="evt-1",
            pattern="呼吸",
            waveform_hash="wave-hash",
            requested_strength=20,
            effective_strength=20,
            active_start_offset_ms=(index - 1) * 1000,
            raw_duration_ms=1000,
            gap_tenths=tenths,
            planned_gap_ms=tenths * 100,
            actual_gap_ms=tenths * 100,
            completed=True,
            interruption_reason=None,
        )
        for index, tenths in enumerate(gap_tenths, 1)
    )
    manifest = ReplayManifest(
        schema_version=SCHEMA_VERSION,
        replay_id="replay-1",
        session_id="session-1",
        seed=7,
        status=session_status,
        mode="autopilot",
    )
    timeline = Timeline(
        schema_version=SCHEMA_VERSION,
        session_id="session-1",
        seed=7,
        plot_events=(),
        cycles=cycles,
    )
    return SimpleNamespace(manifest=manifest, timeline=timeline)
