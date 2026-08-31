"""Per-channel waveform-cycle scheduling with boundary-aware transitions."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from .models import CycleGapPolicy, CycleRecord


class RunnerPhase(str, Enum):
    IDLE = "idle"
    CYCLE = "cycle"
    GAP = "gap"
    PAUSED = "paused"
    STOPPED = "stopped"


@dataclass(frozen=True)
class CycleDirective:
    channel: Literal["A", "B"]
    plot_event_id: str
    pattern: str
    requested_strength: int

    def __post_init__(self) -> None:
        if self.channel not in ("A", "B"):
            raise ValueError("channel must be A or B")
        if not isinstance(self.plot_event_id, str) or not self.plot_event_id:
            raise ValueError("plot_event_id must be a non-empty string")
        if not isinstance(self.pattern, str) or not self.pattern.strip():
            raise ValueError("pattern must be a non-empty string")
        if (
            isinstance(self.requested_strength, bool)
            or not isinstance(self.requested_strength, int)
            or not 0 <= self.requested_strength <= 200
        ):
            raise ValueError("requested_strength must be in 0..200")


@dataclass(frozen=True)
class RunnerState:
    phase: RunnerPhase
    directive: CycleDirective | None
    pending_directive: CycleDirective | None
    cycle_index: int
    generation: int
    failure: str | None
    disconnected: bool


@dataclass
class _CycleDraft:
    directive: CycleDirective
    cycle_index: int
    waveform_hash: str
    active_start_offset_ms: int
    raw_duration_ms: int
    effective_strength: int


class ChannelCycleRunner:
    """Repeat complete raw cycles for one channel without crossing boundaries."""

    def __init__(
        self,
        *,
        channel: Literal["A", "B"],
        policy: CycleGapPolicy,
        rng: Any,
        frames: Mapping[str, Sequence[str]],
        executor: Any,
        clock: Callable[[], float],
        sleeper: Any,
        on_cycle: Callable[[CycleRecord], Any],
    ) -> None:
        if channel not in ("A", "B"):
            raise ValueError("channel must be A or B")
        if not isinstance(policy, CycleGapPolicy):
            raise TypeError("policy must be a CycleGapPolicy")
        normalized_frames: dict[str, tuple[str, ...]] = {}
        for pattern, values in frames.items():
            if not isinstance(pattern, str) or not pattern.strip():
                raise ValueError("waveform pattern names must be non-empty strings")
            if isinstance(values, (str, bytes)):
                raise ValueError("waveform frames must be a sequence of frame strings")
            frame_values = tuple(values)
            if not frame_values or not all(
                isinstance(frame, str) and frame for frame in frame_values
            ):
                raise ValueError("each waveform needs at least one non-empty frame")
            normalized_frames[pattern] = frame_values
        if not normalized_frames:
            raise ValueError("at least one waveform is required")
        if not hasattr(executor, "execute"):
            raise TypeError("executor must provide execute(actions)")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not hasattr(sleeper, "sleep"):
            raise TypeError("sleeper must provide sleep(ms)")
        if not callable(on_cycle):
            raise TypeError("on_cycle must be callable")

        self.channel = channel
        self.policy = policy
        self._rng = rng
        self._frames = normalized_frames
        self._executor = executor
        self._clock = clock
        self._sleeper = sleeper
        self._on_cycle = on_cycle

        self._lock = asyncio.Lock()
        self._worker: asyncio.Task[None] | None = None
        self._gap_sleep: asyncio.Task[None] | None = None
        self._generation = 0
        self._phase = RunnerPhase.IDLE
        self._directive: CycleDirective | None = None
        self._pending: CycleDirective | None = None
        self._cycle_index = 0
        self._failure: str | None = None
        self._disconnected = False
        self._stopped = asyncio.Event()
        self._startup: asyncio.Future[None] | None = None
        self._cancel_reason = "cancelled"
        self._needs_activation = True
        self._effective_strength: int | None = None

        self._draft: _CycleDraft | None = None
        self._raw_completed = False
        self._gap_tenths = 0
        self._planned_gap_ms = 0
        self._gap_started_ms: int | None = None

    def state(self) -> RunnerState:
        return RunnerState(
            phase=self._phase,
            directive=self._directive,
            pending_directive=self._pending,
            cycle_index=self._cycle_index,
            generation=self._generation,
            failure=self._failure,
            disconnected=self._disconnected,
        )

    async def submit(self, directive: CycleDirective) -> None:
        if not isinstance(directive, CycleDirective):
            raise TypeError("directive must be a CycleDirective")
        if directive.channel != self.channel:
            raise ValueError("directive channel does not match runner channel")
        if directive.pattern not in self._frames:
            raise ValueError(f"unknown waveform pattern: {directive.pattern}")

        startup: asyncio.Future[None] | None = None
        async with self._lock:
            if self._phase is RunnerPhase.STOPPED:
                raise RuntimeError("cannot submit to a stopped runner")
            if self._phase is RunnerPhase.PAUSED:
                self._directive = directive
                self._pending = None
                self._needs_activation = True
                return
            if self._worker is None:
                self._directive = directive
                self._pending = None
                self._needs_activation = True
                startup = self._start_worker_locked()
            else:
                self._pending = directive
                if self._phase is RunnerPhase.GAP and self._gap_sleep is not None:
                    self._gap_sleep.cancel()
        if startup is not None:
            await startup

    async def pause(self, *, reason: str = "pause") -> None:
        self._require_reason(reason)
        await self._cancel_worker(RunnerPhase.PAUSED, reason)

    async def resume(self) -> None:
        startup: asyncio.Future[None] | None = None
        async with self._lock:
            if self._phase is RunnerPhase.STOPPED:
                raise RuntimeError("cannot resume a stopped runner")
            if self._phase is not RunnerPhase.PAUSED:
                return
            if self._directive is None:
                self._phase = RunnerPhase.IDLE
                return
            self._needs_activation = True
            self._phase = RunnerPhase.IDLE
            startup = self._start_worker_locked()
        if startup is not None:
            await startup

    async def stop(self, *, clear: bool = False, reason: str = "stop") -> None:
        self._require_reason(reason)
        await self._cancel_worker(RunnerPhase.STOPPED, reason)
        if clear:
            try:
                executed, dropped = await self._executor.execute(
                    [{"op": "clear", "channel": self.channel}]
                )
                if dropped or not self._action_was_executed(
                    executed, {"op": "clear", "channel": self.channel}
                ):
                    self._failure = self._format_rejection(dropped, "clear was not executed")
                    self._disconnected = self._is_disconnect(dropped)
            except Exception as exc:  # the stopped phase remains authoritative
                self._failure = str(exc) or type(exc).__name__
                self._disconnected = isinstance(exc, ConnectionError) or self._is_disconnect(exc)
        self._stopped.set()

    async def wait_stopped(self) -> RunnerState:
        await self._stopped.wait()
        return self.state()

    def _start_worker_locked(self) -> asyncio.Future[None]:
        self._generation += 1
        generation = self._generation
        self._failure = None
        self._disconnected = False
        self._stopped.clear()
        self._startup = asyncio.get_running_loop().create_future()
        self._worker = asyncio.create_task(
            self._run(generation), name=f"timeline-cycle-{self.channel}"
        )
        return self._startup

    async def _cancel_worker(self, target: RunnerPhase, reason: str) -> None:
        worker: asyncio.Task[None] | None
        async with self._lock:
            self._generation += 1
            self._phase = target
            self._pending = None
            self._cancel_reason = reason
            self._needs_activation = True
            worker = self._worker
            if worker is not None and not worker.done():
                worker.cancel()
        if worker is not None:
            try:
                await worker
            except asyncio.CancelledError:
                pass

    async def _run(self, generation: int) -> None:
        try:
            while await self._generation_is_current(generation):
                directive = self._directive
                if directive is None:
                    self._phase = RunnerPhase.IDLE
                    return

                frames = self._frames[directive.pattern]
                raw_duration_ms = self.policy.cycle_ms(len(frames))
                self._cycle_index += 1
                self._phase = RunnerPhase.CYCLE
                self._draft = _CycleDraft(
                    directive=directive,
                    cycle_index=self._cycle_index,
                    waveform_hash=self._waveform_hash(frames),
                    active_start_offset_ms=self._now_ms(),
                    raw_duration_ms=raw_duration_ms,
                    effective_strength=(
                        directive.requested_strength
                        if self._needs_activation or self._effective_strength is None
                        else self._effective_strength
                    ),
                )
                self._raw_completed = False
                self._gap_tenths = 0
                self._planned_gap_ms = 0
                self._gap_started_ms = None

                actions: list[dict[str, Any]] = []
                if self._needs_activation:
                    actions.append(
                        {
                            "op": "hold_strength",
                            "channel": self.channel,
                            "value": directive.requested_strength,
                        }
                    )
                pulse = {
                    "op": "pulse_cycle",
                    "channel": self.channel,
                    "pattern": directive.pattern,
                }
                actions.append(pulse)

                try:
                    executed, dropped = await self._executor.execute(actions)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._stop_for_failure(
                        exc, interruption_reason="executor_failure"
                    )
                    return

                if dropped or not all(
                    self._action_was_executed(executed, action) for action in actions
                ) or not self._action_was_executed(executed, pulse):
                    await self._stop_for_failure(
                        self._format_rejection(dropped, "requested action was not executed"),
                        interruption_reason="executor_rejected",
                        disconnected=self._is_disconnect(dropped),
                    )
                    return

                if self._needs_activation:
                    self._effective_strength = self._read_effective_strength(
                        executed, directive.requested_strength
                    )
                    self._draft.effective_strength = self._effective_strength
                self._needs_activation = False
                self._resolve_startup()

                await self._sleeper.sleep(raw_duration_ms)
                self._raw_completed = True
                if not await self._generation_is_current(generation):
                    return

                pending = await self._take_pending(generation)
                if pending is not None:
                    await self._emit_current(completed=True, interruption_reason=None)
                    self._directive = pending
                    self._needs_activation = True
                    continue

                self._gap_tenths = self.policy.sample_tenths(self._rng)
                self._planned_gap_ms = self.policy.gap_ms(
                    len(frames), self._gap_tenths
                )
                if self._planned_gap_ms == 0:
                    await self._emit_current(completed=True, interruption_reason=None)
                    await asyncio.sleep(0)
                    continue

                self._phase = RunnerPhase.GAP
                self._gap_started_ms = self._now_ms()
                self._gap_sleep = asyncio.create_task(
                    self._sleeper.sleep(self._planned_gap_ms),
                    name=f"timeline-gap-{self.channel}",
                )
                gap_interrupted = False
                try:
                    await self._gap_sleep
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    gap_interrupted = True
                finally:
                    self._gap_sleep = None

                if not await self._generation_is_current(generation):
                    return
                pending = await self._take_pending(generation)
                await self._emit_current(
                    completed=True,
                    interruption_reason="event_change" if gap_interrupted else None,
                )
                if pending is not None:
                    self._directive = pending
                    self._needs_activation = True
                    continue
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            if self._draft is not None:
                await self._emit_current(
                    completed=self._raw_completed,
                    interruption_reason=self._cancel_reason,
                )
        finally:
            self._resolve_startup()
            if self._worker is asyncio.current_task():
                self._worker = None

    async def _generation_is_current(self, generation: int) -> bool:
        async with self._lock:
            return (
                generation == self._generation
                and self._phase not in (RunnerPhase.PAUSED, RunnerPhase.STOPPED)
            )

    async def _take_pending(self, generation: int) -> CycleDirective | None:
        async with self._lock:
            if generation != self._generation:
                return None
            pending = self._pending
            self._pending = None
            return pending

    async def _stop_for_failure(
        self,
        failure: object,
        *,
        interruption_reason: str,
        disconnected: bool | None = None,
    ) -> None:
        self._failure = str(failure) or type(failure).__name__
        self._disconnected = (
            self._is_disconnect(failure) if disconnected is None else disconnected
        )
        self._phase = RunnerPhase.STOPPED
        self._generation += 1
        await self._emit_current(
            completed=False, interruption_reason=interruption_reason
        )
        self._stopped.set()
        self._resolve_startup()

    async def _emit_current(
        self, *, completed: bool, interruption_reason: str | None
    ) -> None:
        draft = self._draft
        if draft is None:
            return
        actual_gap_ms = 0
        if self._gap_started_ms is not None:
            actual_gap_ms = max(0, self._now_ms() - self._gap_started_ms)
            actual_gap_ms = min(actual_gap_ms, self._planned_gap_ms)
        record = CycleRecord(
            channel=self.channel,
            cycle_index=draft.cycle_index,
            plot_event_id=draft.directive.plot_event_id,
            pattern=draft.directive.pattern,
            waveform_hash=draft.waveform_hash,
            requested_strength=draft.directive.requested_strength,
            effective_strength=draft.effective_strength,
            active_start_offset_ms=draft.active_start_offset_ms,
            raw_duration_ms=draft.raw_duration_ms,
            gap_tenths=self._gap_tenths,
            planned_gap_ms=self._planned_gap_ms,
            actual_gap_ms=actual_gap_ms,
            completed=completed,
            interruption_reason=interruption_reason,
        )
        self._draft = None
        result = self._on_cycle(record)
        if inspect.isawaitable(result):
            await result

    def _resolve_startup(self) -> None:
        startup = self._startup
        if startup is not None and not startup.done():
            startup.set_result(None)
        self._startup = None

    def _now_ms(self) -> int:
        return max(0, int(round(self._clock() * 1000)))

    @staticmethod
    def _waveform_hash(frames: Sequence[str]) -> str:
        return hashlib.sha256("\0".join(frames).encode("utf-8")).hexdigest()

    @staticmethod
    def _action_was_executed(executed: object, expected: Mapping[str, Any]) -> bool:
        if not isinstance(executed, Sequence) or isinstance(executed, (str, bytes)):
            return False
        return any(
            isinstance(item, Mapping)
            and isinstance(item.get("action"), Mapping)
            and dict(item["action"]) == dict(expected)
            for item in executed
        )

    @staticmethod
    def _read_effective_strength(executed: object, fallback: int) -> int:
        if isinstance(executed, Sequence) and not isinstance(executed, (str, bytes)):
            for item in executed:
                if not isinstance(item, Mapping):
                    continue
                action = item.get("action")
                effective = item.get("effective")
                if (
                    isinstance(action, Mapping)
                    and action.get("op") == "hold_strength"
                    and isinstance(effective, Mapping)
                ):
                    value = effective.get("effective_strength")
                    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 200:
                        return value
        return fallback

    @staticmethod
    def _format_rejection(dropped: object, fallback: str) -> str:
        if isinstance(dropped, Sequence) and not isinstance(dropped, (str, bytes)):
            reasons = [
                str(item.get("reason"))
                for item in dropped
                if isinstance(item, Mapping) and item.get("reason")
            ]
            if reasons:
                return "; ".join(reasons)
        return fallback

    @staticmethod
    def _is_disconnect(value: object) -> bool:
        text = str(value).casefold()
        return any(
            marker in text
            for marker in (
                "disconnect",
                "not connected",
                "connection lost",
                "未连接",
                "断开",
            )
        )

    @staticmethod
    def _require_reason(reason: str) -> None:
        if not isinstance(reason, str) or not reason:
            raise ValueError("reason must be a non-empty string")
