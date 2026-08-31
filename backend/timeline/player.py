"""Exact monotonic playback of completed recorded waveform cycles."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .models import CycleRecord, SessionStatus, Timeline


class ReplayPlaybackError(RuntimeError):
    """Recorded playback could not continue through the current executor."""


class _AsyncioSleeper:
    async def sleep(self, ms: int) -> None:
        await asyncio.sleep(ms / 1000)


class RecordedCyclePlayer:
    """Play immutable recorded starts without constructing or sampling an RNG."""

    def __init__(
        self,
        *,
        executor: Any,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Any | None = None,
    ) -> None:
        if not hasattr(executor, "execute_actions"):
            raise TypeError("executor must provide execute_actions(actions)")
        if not hasattr(executor, "clear_output"):
            raise TypeError("executor must provide clear_output(channel=None)")
        if not callable(clock):
            raise TypeError("clock must be callable")
        sleeper = _AsyncioSleeper() if sleeper is None else sleeper
        if not hasattr(sleeper, "sleep"):
            raise TypeError("sleeper must provide sleep(ms)")

        self._executor = executor
        self._clock = clock
        self._sleeper = sleeper
        self._ordered_cycles: tuple[CycleRecord, ...] = ()
        self._task: asyncio.Task[None] | None = None
        self._cursor = 0
        self._adjusted = False
        self._loaded = False
        self._running = False
        self._paused = False
        self._stopped = False
        self._cleared = False
        self._active_elapsed_ms = 0
        self._segment_started_at: float | None = None
        self._failure: ReplayPlaybackError | None = None
        self._lock = asyncio.Lock()
        self._clear_lock = asyncio.Lock()

    @property
    def ordered_cycles(self) -> tuple[CycleRecord, ...]:
        return self._ordered_cycles

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def adjusted(self) -> bool:
        return self._adjusted

    @property
    def running(self) -> bool:
        return self._running

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def failure(self) -> ReplayPlaybackError | None:
        return self._failure

    def validate_cursor(self, cursor: int) -> None:
        if (
            isinstance(cursor, bool)
            or not isinstance(cursor, int)
            or not 0 <= cursor <= len(self._ordered_cycles)
        ):
            raise ValueError("cursor is outside the recorded cycle range")

    def load(self, replay: Any) -> None:
        """Load one validated completed replay bundle or timeline while idle."""
        if self._task is not None and not self._task.done():
            raise RuntimeError("cannot load replay while playback is active")
        if self._task is not None and not self._task.cancelled():
            self._task.exception()
        manifest = getattr(replay, "manifest", None)
        timeline = getattr(replay, "timeline", replay)
        if (
            manifest is not None
            and getattr(manifest, "status", None) is not SessionStatus.COMPLETED
        ):
            raise ValueError("replay must be a completed session")
        if not isinstance(timeline, Timeline):
            raise TypeError("replay must provide a Timeline")

        completed = [cycle for cycle in timeline.cycles if cycle.completed]
        self._ordered_cycles = self._order_cycles(completed)
        self._task = None
        self._cursor = 0
        self._adjusted = False
        self._loaded = True
        self._running = False
        self._paused = False
        self._stopped = False
        self._cleared = False
        self._active_elapsed_ms = 0
        self._segment_started_at = None
        self._failure = None

    async def start(self) -> None:
        async with self._lock:
            if not self._loaded:
                raise RuntimeError("no replay is loaded")
            if self._task is not None and not self._task.done():
                return
            if self._stopped:
                raise RuntimeError("stopped playback must be loaded again")
            self._paused = False
            self._running = True
            self._cleared = False
            self._failure = None
            self._segment_started_at = self._clock()
            self._task = asyncio.create_task(
                self._run(), name="timeline-recorded-cycle-player"
            )

    async def pause(self) -> int:
        async with self._lock:
            if self._stopped and self._cleared:
                return self._cursor
            task = self._task
            if self._paused and self._cleared:
                return self._cursor
            if task is None and not self._running:
                return self._cursor
            if not self._paused:
                self._capture_active_elapsed()
                self._paused = True
                self._running = False
                if task is not None and not task.done():
                    task.cancel()
            await self._await_terminal(task)
            await self._clear_once()
            return self._cursor

    async def resume(self, cursor: int | None = None) -> None:
        async with self._lock:
            if not self._loaded:
                raise RuntimeError("no replay is loaded")
            if cursor is not None:
                self.validate_cursor(cursor)
            if self._stopped:
                raise RuntimeError("cannot resume stopped playback")
            if self._running:
                return
            if self._task is not None and not self._cleared:
                raise RuntimeError("playback output clear is still pending")
            if cursor is not None:
                self._cursor = cursor
                self._active_elapsed_ms = (
                    self._ordered_cycles[cursor].active_start_offset_ms
                    if cursor < len(self._ordered_cycles)
                    else self._playback_end_offset_ms()
                )
            self._paused = False
            self._running = True
            self._cleared = False
            self._failure = None
            self._segment_started_at = self._clock()
            self._task = asyncio.create_task(
                self._run(), name="timeline-recorded-cycle-player"
            )

    async def stop(self) -> None:
        async with self._lock:
            if self._stopped and self._cleared:
                return
            task = self._task
            if not self._stopped:
                self._capture_active_elapsed()
                self._running = False
                self._paused = False
                self._stopped = True
                if task is not None and not task.done():
                    task.cancel()
            await self._await_terminal(task)
            await self._clear_once()

    async def wait(self) -> None:
        task = self._task
        if task is not None:
            await task

    async def _run(self) -> None:
        try:
            while self._cursor < len(self._ordered_cycles):
                record = self._ordered_cycles[self._cursor]
                remaining_ms = record.active_start_offset_ms - self._active_now_ms()
                if remaining_ms > 0:
                    await self._sleeper.sleep(remaining_ms)
                await self._play_cycle(record)
                self._cursor += 1
            final_remaining_ms = self._playback_end_offset_ms() - self._active_now_ms()
            if final_remaining_ms > 0:
                await self._sleeper.sleep(final_remaining_ms)
            self._running = False
            self._segment_started_at = None
            await self._clear_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._capture_active_elapsed()
            self._running = False
            self._failure = (
                exc
                if isinstance(exc, ReplayPlaybackError)
                else ReplayPlaybackError(str(exc) or type(exc).__name__)
            )
            try:
                await self._clear_once()
            except Exception as clear_exc:
                self._failure = ReplayPlaybackError(
                    f"{self._failure}; clear failed: {clear_exc}"
                )
            if self._failure is exc:
                raise
            raise self._failure from exc

    async def _play_cycle(self, record: CycleRecord) -> None:
        actions = [
            {
                "op": "hold_strength",
                "channel": record.channel,
                "value": record.requested_strength,
            },
            {
                "op": "pulse_cycle",
                "channel": record.channel,
                "pattern": record.pattern,
            },
        ]
        try:
            executed, dropped = await self._executor.execute_actions(actions)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ReplayPlaybackError(f"executor failure: {exc}") from exc

        strength_result = self._effective_for(executed, actions[0])
        cycle_result = self._effective_for(executed, actions[1])
        if dropped or strength_result is None:
            self._adjusted = True
        if cycle_result is None:
            raise ReplayPlaybackError("recorded cycle action was not executed")

        expected_strength = {
            "op": "hold_strength",
            "channel": record.channel,
            "requested_strength": record.requested_strength,
            "effective_strength": record.effective_strength,
        }
        expected_cycle = {
            "op": "pulse_cycle",
            "channel": record.channel,
            "pattern": record.pattern,
            "effective_strength": record.effective_strength,
            "duration_ms": record.raw_duration_ms,
        }
        if strength_result is None or any(
            strength_result.get(key) != value
            for key, value in expected_strength.items()
        ):
            self._adjusted = True
        if any(cycle_result.get(key) != value for key, value in expected_cycle.items()):
            self._adjusted = True

    async def _clear_once(self) -> None:
        async with self._clear_lock:
            if self._cleared:
                return
            await self._executor.clear_output()
            self._cleared = True

    def _active_now_ms(self) -> int:
        if self._segment_started_at is None:
            return self._active_elapsed_ms
        elapsed = max(0, int(round((self._clock() - self._segment_started_at) * 1000)))
        return self._active_elapsed_ms + elapsed

    def _capture_active_elapsed(self) -> None:
        if self._segment_started_at is not None:
            self._active_elapsed_ms = self._active_now_ms()
            self._segment_started_at = None

    def _playback_end_offset_ms(self) -> int:
        return max(
            (
                cycle.active_start_offset_ms + cycle.raw_duration_ms
                for cycle in self._ordered_cycles
            ),
            default=0,
        )

    @staticmethod
    def _effective_for(
        executed: object, expected_action: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        if not isinstance(executed, Sequence) or isinstance(executed, (str, bytes)):
            return None
        for item in executed:
            if not isinstance(item, Mapping):
                continue
            action = item.get("action")
            effective = item.get("effective")
            if (
                isinstance(action, Mapping)
                and dict(action) == dict(expected_action)
                and isinstance(effective, Mapping)
                and effective.get("op") == expected_action.get("op")
                and effective.get("channel") == expected_action.get("channel")
            ):
                return effective
        return None

    @staticmethod
    def _order_cycles(cycles: Sequence[CycleRecord]) -> tuple[CycleRecord, ...]:
        archive_positions = {id(cycle): index for index, cycle in enumerate(cycles)}
        by_channel: dict[str, list[CycleRecord]] = {"A": [], "B": []}
        for cycle in cycles:
            by_channel[cycle.channel].append(cycle)
        for channel_cycles in by_channel.values():
            channel_cycles.sort(
                key=lambda cycle: (cycle.cycle_index, archive_positions[id(cycle)])
            )
            if any(
                current.cycle_index == previous.cycle_index
                for previous, current in zip(channel_cycles, channel_cycles[1:])
            ):
                raise ValueError("replay contains a duplicate channel cycle index")
            if any(
                current.active_start_offset_ms < previous.active_start_offset_ms
                for previous, current in zip(channel_cycles, channel_cycles[1:])
            ):
                raise ValueError("replay channel cycle offsets are not monotonic")

        positions = {"A": 0, "B": 0}
        ordered: list[CycleRecord] = []
        while len(ordered) < len(cycles):
            candidates = [
                channel_cycles[positions[channel]]
                for channel, channel_cycles in by_channel.items()
                if positions[channel] < len(channel_cycles)
            ]
            next_cycle = min(
                candidates,
                key=lambda cycle: (
                    cycle.active_start_offset_ms,
                    archive_positions[id(cycle)],
                ),
            )
            ordered.append(next_cycle)
            positions[next_cycle.channel] += 1
        return tuple(ordered)

    @staticmethod
    async def _await_terminal(task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            pass
        except ReplayPlaybackError:
            pass
