"""Exact monotonic playback of completed recorded waveform cycles."""

from __future__ import annotations

import asyncio
import hashlib
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
        self._clear_pending = False
        self._active_elapsed_ms = 0
        self._segment_started_at: float | None = None
        self._failure: ReplayPlaybackError | None = None
        self._started_cycles: dict[str, tuple[int, CycleRecord, int]] = {}
        self._owner_generations: dict[str, int] | None = None
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

    @property
    def cleared(self) -> bool:
        return self._cleared

    def channel_states(self) -> dict[str, dict[str, Any]]:
        """Return redacted per-channel replay progress for public state."""
        states: dict[str, dict[str, Any]] = {}
        now_ms = self._active_now_ms()
        for channel in ("A", "B"):
            phase = (
                "stopped"
                if self._clear_pending
                else "paused"
                if self._paused
                else "idle"
            )
            pattern: str | None = None
            strength = 0
            cycle_index = 0
            next_cycle_start_ms: int | None = None
            started = self._started_cycles.get(channel)
            if self._running and started is not None:
                ordered_index, record, effective_strength = started
                raw_end = record.active_start_offset_ms + record.raw_duration_ms
                gap_end = raw_end + record.actual_gap_ms
                if now_ms < raw_end:
                    phase = "cycle"
                elif now_ms < gap_end:
                    phase = "gap"
                pattern = record.pattern if phase in ("cycle", "gap") else None
                strength = effective_strength if pattern is not None else 0
                cycle_index = record.cycle_index
                next_cycle_start_ms = self._next_channel_start(
                    channel, ordered_index
                )
            elif self._running:
                next_cycle_start_ms = self._next_channel_start(
                    channel, self._cursor - 1
                )
            states[channel] = {
                "phase": phase,
                "pattern": pattern,
                "strength": strength,
                "cycle_index": cycle_index,
                "next_cycle_start_ms": next_cycle_start_ms,
            }
        return states

    def validate_cursor(self, cursor: int) -> None:
        if (
            isinstance(cursor, bool)
            or not isinstance(cursor, int)
            or not 0 <= cursor <= len(self._ordered_cycles)
        ):
            raise ValueError("cursor is outside the recorded cycle range")

    def set_output_generations(
        self, generations: Mapping[str, int]
    ) -> None:
        normalized = {
            str(channel): int(generation)
            for channel, generation in generations.items()
        }
        if set(normalized) != {"A", "B"}:
            raise ValueError("replay output generations must provide A and B")
        self._owner_generations = normalized

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
        self._clear_pending = False
        self._active_elapsed_ms = 0
        self._segment_started_at = None
        self._failure = None
        self._started_cycles = {}
        self._owner_generations = None

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
            self._clear_pending = False
            self._failure = None
            self._segment_started_at = self._clock()
            self._task = asyncio.create_task(
                self._run(), name="timeline-recorded-cycle-player"
            )

    async def pause(self) -> int:
        async with self._lock:
            if self._stopped and self._clear_cache_is_current():
                return self._cursor
            task = self._task
            if self._paused and self._clear_cache_is_current():
                return self._cursor
            if (
                task is None
                and not self._running
                and self._clear_cache_is_current()
            ):
                return self._cursor
            if self._running:
                self._capture_active_elapsed()
                self._rewind_interrupted_cycles()
                self._running = False
                if task is not None and not task.done():
                    task.cancel()
            await self._await_terminal(task)
            await self._clear_once()
            self._paused = True
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
            self._clear_pending = False
            self._failure = None
            self._started_cycles = {}
            self._segment_started_at = self._clock()
            self._task = asyncio.create_task(
                self._run(), name="timeline-recorded-cycle-player"
            )

    async def stop(self) -> None:
        async with self._lock:
            if self._stopped and self._clear_cache_is_current():
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
                ordered_index = self._cursor
                record = self._ordered_cycles[self._cursor]
                remaining_ms = record.active_start_offset_ms - self._active_now_ms()
                if remaining_ms > 0:
                    await self._sleeper.sleep(remaining_ms)
                effective_strength = await self._play_cycle(record)
                self._started_cycles[record.channel] = (
                    ordered_index,
                    record,
                    effective_strength,
                )
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

    async def _play_cycle(self, record: CycleRecord) -> int:
        authoritative_hash = self._authoritative_waveform_hash(record.pattern)
        if authoritative_hash is None:
            raise ReplayPlaybackError(
                f"authoritative waveform is unavailable: {record.pattern}"
            )
        if authoritative_hash != record.waveform_hash:
            self._adjusted = True
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
            execute_timeline = getattr(
                self._executor, "execute_timeline_actions", None
            )
            if callable(execute_timeline) and self._owner_generations is not None:
                executed, dropped = await execute_timeline(
                    actions, dict(self._owner_generations)
                )
            else:
                executed, dropped = await self._executor.execute_actions(actions)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ReplayPlaybackError(f"executor failure: {exc}") from exc

        strength_result = self._effective_for(executed, actions[0])
        cycle_result = self._effective_for(executed, actions[1])
        if strength_result is None:
            raise ReplayPlaybackError(
                "recorded strength prerequisite was not confirmed"
            )
        if dropped:
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
        value = cycle_result.get("effective_strength")
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    async def _clear_once(self) -> None:
        async with self._clear_lock:
            if self._clear_cache_is_current():
                return
            self._cleared = False
            self._clear_pending = True
            require_clear = getattr(
                self._executor, "require_output_clear", None
            )
            if callable(require_clear):
                require_clear(("A", "B"))
            result = await self._executor.clear_output()
            if not self._clear_was_executed(result):
                raise ReplayPlaybackError("recorded playback clear was not confirmed")
            self._cleared = True
            self._clear_pending = False

    def _clear_cache_is_current(self) -> bool:
        if not self._cleared:
            return False
        is_confirmed = getattr(
            self._executor, "output_clear_is_confirmed", None
        )
        if not callable(is_confirmed):
            return True
        try:
            return bool(is_confirmed(("A", "B")))
        except Exception:
            return False

    @staticmethod
    def _clear_was_executed(result: object) -> bool:
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or not isinstance(result[0], Sequence)
            or isinstance(result[0], (str, bytes))
            or not isinstance(result[1], Sequence)
            or isinstance(result[1], (str, bytes))
            or result[1]
        ):
            return False
        expected = {"op": "stop"}
        return any(
            isinstance(item, Mapping)
            and isinstance(item.get("action"), Mapping)
            and dict(item["action"]) == expected
            for item in result[0]
        )

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
                cycle.active_start_offset_ms
                + cycle.raw_duration_ms
                + cycle.actual_gap_ms
                for cycle in self._ordered_cycles
            ),
            default=0,
        )

    def _rewind_interrupted_cycles(self) -> None:
        interrupted = [
            (ordered_index, record)
            for ordered_index, record, _strength in self._started_cycles.values()
            if (
                record.active_start_offset_ms
                <= self._active_elapsed_ms
                < record.active_start_offset_ms + record.raw_duration_ms
            )
        ]
        if interrupted:
            ordered_index, record = min(interrupted, key=lambda value: value[0])
            self._cursor = min(self._cursor, ordered_index)
            self._active_elapsed_ms = min(
                self._active_elapsed_ms, record.active_start_offset_ms
            )
        self._started_cycles = {}

    def _next_channel_start(
        self, channel: str, after_ordered_index: int
    ) -> int | None:
        for index, record in enumerate(self._ordered_cycles):
            if index > after_ordered_index and record.channel == channel:
                return record.active_start_offset_ms
        return None

    def _authoritative_waveform_hash(self, pattern: str) -> str | None:
        safety = getattr(self._executor, "safety", None)
        presets = getattr(safety, "presets", None)
        if not isinstance(presets, Mapping):
            return None
        metadata = presets.get(pattern)
        if not isinstance(metadata, Mapping):
            return None
        frames = metadata.get("frames")
        if (
            not isinstance(frames, Sequence)
            or isinstance(frames, (str, bytes))
            or not frames
            or not all(isinstance(frame, str) and frame for frame in frames)
        ):
            return None
        return hashlib.sha256("\0".join(frames).encode("utf-8")).hexdigest()

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
