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
    worker_active: bool
    next_cycle_start_ms: int | None = None


@dataclass
class _CycleAttempt:
    directive: CycleDirective
    cycle_index: int
    waveform_hash: str
    active_start_offset_ms: int
    raw_duration_ms: int
    effective_strength: int
    raw_completed: bool = False
    gap_tenths: int = 0
    planned_gap_ms: int = 0
    gap_started_ms: int | None = None


class _CycleCallbackError(RuntimeError):
    pass


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
        self._delivery_lock = asyncio.Lock()
        self._delivery_owner: asyncio.Task[Any] | None = None
        self._pending_records: dict[tuple[str, int], CycleRecord] = {}
        self._worker: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._stop_clear_task: asyncio.Task[None] | None = None
        self._stop_invalidation: asyncio.Future[None] | None = None
        self._cancel_reasons: dict[asyncio.Task[None], str] = {}
        self._gap_sleep: asyncio.Task[None] | None = None
        self._generation = 0
        self._phase = RunnerPhase.IDLE
        self._directive: CycleDirective | None = None
        self._pending: CycleDirective | None = None
        self._cycle_index = 0
        self._failure: str | None = None
        self._disconnected = False
        self._stopped = asyncio.Event()
        self._needs_activation = True
        self._effective_strength: int | None = None
        self._next_cycle_start_ms: int | None = None

    def state(self) -> RunnerState:
        return RunnerState(
            phase=self._phase,
            directive=self._directive,
            pending_directive=self._pending,
            cycle_index=self._cycle_index,
            generation=self._generation,
            failure=self._failure,
            disconnected=self._disconnected,
            worker_active=self._worker is not None and not self._worker.done(),
            next_cycle_start_ms=(
                self._next_cycle_start_ms
                if self._phase is RunnerPhase.GAP
                else None
            ),
        )

    def pending_records(self) -> tuple[CycleRecord, ...]:
        """Return records whose callback delivery has not succeeded."""
        return tuple(self._pending_records.values())

    async def retry_pending_records(self) -> None:
        """Retry durable delivery without changing terminal runner state."""
        await self._start_record_delivery()

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
        worker = await self._invalidate_worker(RunnerPhase.PAUSED, reason)
        await self._wait_worker(worker)

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
        async with self._lock:
            stop_task = self._stop_task
            if stop_task is None or stop_task.done():
                invalidation = asyncio.get_running_loop().create_future()
                stop_task = asyncio.create_task(
                    self._finish_stop(reason=reason, invalidation=invalidation),
                    name=f"timeline-stop-{self.channel}",
                )
                self._stop_task = stop_task
                self._stop_invalidation = invalidation
                self._stop_clear_task = None
                stop_task.add_done_callback(self._stop_finished)
            else:
                invalidation = self._stop_invalidation
                if invalidation is None:
                    raise RuntimeError("active stop is missing invalidation state")
            clear_task = self._stop_clear_task
            if clear and clear_task is None:
                clear_task = asyncio.create_task(
                    self._clear_after_invalidation(invalidation),
                    name=f"timeline-clear-{self.channel}",
                )
                self._stop_clear_task = clear_task
        if clear_task is not None:
            await asyncio.shield(clear_task)
        await asyncio.shield(stop_task)

    async def _finish_stop(
        self, *, reason: str, invalidation: asyncio.Future[None]
    ) -> None:
        worker: asyncio.Task[None] | None = None
        try:
            worker = await self._invalidate_worker(RunnerPhase.STOPPED, reason)
            if not invalidation.done():
                invalidation.set_result(None)
            clear_task = self._stop_clear_task
            if clear_task is not None:
                await asyncio.shield(clear_task)
            await self._wait_worker(worker)
        finally:
            try:
                if not invalidation.done():
                    invalidation.set_result(None)
                late_clear_task = self._stop_clear_task
                if late_clear_task is not None:
                    await asyncio.shield(late_clear_task)
            finally:
                self._stopped.set()

    async def _clear_after_invalidation(
        self, invalidation: asyncio.Future[None]
    ) -> None:
        await asyncio.shield(invalidation)
        try:
            executed, dropped = await self._executor.execute(
                [{"op": "clear", "channel": self.channel}]
            )
            if dropped or not self._action_was_executed(
                executed, {"op": "clear", "channel": self.channel}
            ):
                self._failure = self._format_rejection(
                    dropped, "clear was not executed"
                )
                self._disconnected = self._is_disconnect(dropped)
        except Exception as exc:  # the stopped phase remains authoritative
            self._failure = str(exc) or type(exc).__name__
            self._disconnected = isinstance(
                exc, ConnectionError
            ) or self._is_disconnect(exc)

    async def wait_stopped(self) -> RunnerState:
        await self._stopped.wait()
        return self.state()

    def _start_worker_locked(self) -> asyncio.Future[None]:
        self._generation += 1
        generation = self._generation
        self._failure = None
        self._disconnected = False
        self._stopped.clear()
        startup = asyncio.get_running_loop().create_future()
        worker = asyncio.create_task(
            self._run(generation, startup), name=f"timeline-cycle-{self.channel}"
        )
        self._worker = worker
        self._cancel_reasons[worker] = "cancelled"
        worker.add_done_callback(
            lambda completed: self._worker_finished(completed, startup)
        )
        return startup

    async def _invalidate_worker(
        self, target: RunnerPhase, reason: str
    ) -> asyncio.Task[None] | None:
        async with self._lock:
            if self._phase is RunnerPhase.STOPPED:
                return self._worker if target is RunnerPhase.STOPPED else None
            self._generation += 1
            self._phase = target
            self._pending = None
            self._needs_activation = True
            worker = self._worker
            if worker is not None and not worker.done():
                self._cancel_reasons[worker] = reason
                worker.cancel()
            return worker

    @staticmethod
    async def _wait_worker(worker: asyncio.Task[None] | None) -> None:
        if worker is not None:
            try:
                await worker
            except asyncio.CancelledError:
                pass

    async def _run(
        self, generation: int, startup: asyncio.Future[None]
    ) -> None:
        attempt: _CycleAttempt | None = None
        try:
            while True:
                directive = await self._prepare_next_directive(generation)
                if directive is None:
                    return

                frames = self._frames[directive.pattern]
                raw_duration_ms = self.policy.cycle_ms(len(frames))
                self._cycle_index += 1
                self._phase = RunnerPhase.CYCLE
                self._next_cycle_start_ms = None
                attempt = _CycleAttempt(
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

                activating = self._needs_activation
                actions: list[dict[str, Any]] = []
                if activating:
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
                if not activating:
                    # A continuation reuses the strength confirmed by this
                    # runner's current owner generation.  Initial activations
                    # deliberately omit this marker and must carry a hold.
                    pulse["_strength_prerequisite_confirmed"] = True
                actions.append(pulse)

                try:
                    executed, dropped = await self._executor.execute(actions)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._resolve_startup(startup)
                    attempt = None
                    await self._stop_for_failure(exc)
                    return

                if not await self._generation_is_current(generation):
                    return
                if self._needs_activation:
                    try:
                        self._effective_strength = self._read_effective_strength(
                            executed, actions[0]
                        )
                    except ValueError as exc:
                        self._resolve_startup(startup)
                        attempt = None
                        await self._stop_for_failure(exc)
                        return
                    attempt.effective_strength = self._effective_strength
                if dropped or not all(
                    self._action_was_executed(executed, action) for action in actions
                ) or not self._action_was_executed(executed, pulse):
                    self._resolve_startup(startup)
                    attempt = None
                    await self._stop_for_failure(
                        self._format_rejection(dropped, "requested action was not executed"),
                        disconnected=self._is_disconnect(dropped),
                    )
                    return

                self._needs_activation = False
                self._resolve_startup(startup)

                await self._sleeper.sleep(raw_duration_ms)
                attempt.raw_completed = True
                if not await self._generation_is_current(generation):
                    return

                pending = await self._take_pending(generation)
                if pending is not None:
                    record = self._record_from_attempt(
                        attempt, completed=True, interruption_reason=None
                    )
                    attempt = None
                    await self._deliver_record(record)
                    self._directive = pending
                    self._needs_activation = True
                    continue

                attempt.gap_tenths = self.policy.sample_tenths(self._rng)
                attempt.planned_gap_ms = self.policy.gap_ms(
                    len(frames), attempt.gap_tenths
                )
                if attempt.planned_gap_ms == 0:
                    record = self._record_from_attempt(
                        attempt, completed=True, interruption_reason=None
                    )
                    attempt = None
                    await self._deliver_record(record)
                    await asyncio.sleep(0)
                    continue

                self._phase = RunnerPhase.GAP
                attempt.gap_started_ms = self._now_ms()
                self._next_cycle_start_ms = (
                    attempt.gap_started_ms + attempt.planned_gap_ms
                )
                gap_sleep = asyncio.create_task(
                    self._sleeper.sleep(attempt.planned_gap_ms),
                    name=f"timeline-gap-{self.channel}",
                )
                self._gap_sleep = gap_sleep
                gap_interrupted = False
                try:
                    await gap_sleep
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    gap_interrupted = True
                finally:
                    if self._gap_sleep is gap_sleep:
                        self._gap_sleep = None

                if not await self._generation_is_current(generation):
                    return
                pending = await self._take_pending(generation)
                record = self._record_from_attempt(
                    attempt,
                    completed=True,
                    interruption_reason="event_change" if gap_interrupted else None,
                )
                attempt = None
                await self._deliver_record(record)
                if pending is not None:
                    self._directive = pending
                    self._needs_activation = True
                    continue
                await asyncio.sleep(0)
        except _CycleCallbackError as exc:
            await self._stop_for_failure(
                exc,
            )
        except asyncio.CancelledError:
            try:
                if attempt is not None:
                    task = asyncio.current_task()
                    reason = (
                        self._cancel_reasons.get(task, "cancelled")
                        if task is not None
                        else "cancelled"
                    )
                    record = self._record_from_attempt(
                        attempt,
                        completed=attempt.raw_completed,
                        interruption_reason=reason,
                    )
                    attempt = None
                    await self._deliver_record(record)
            except _CycleCallbackError as exc:
                await self._stop_for_failure(
                    exc,
                )
        finally:
            self._resolve_startup(startup)

    async def _generation_is_current(self, generation: int) -> bool:
        async with self._lock:
            return (
                generation == self._generation
                and self._phase not in (RunnerPhase.PAUSED, RunnerPhase.STOPPED)
            )

    async def _prepare_next_directive(
        self, generation: int
    ) -> CycleDirective | None:
        async with self._lock:
            if (
                generation != self._generation
                or self._phase in (RunnerPhase.PAUSED, RunnerPhase.STOPPED)
            ):
                return None
            if self._pending is not None:
                self._directive = self._pending
                self._pending = None
                self._needs_activation = True
            if self._directive is None:
                self._phase = RunnerPhase.IDLE
            return self._directive

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
        disconnected: bool | None = None,
        record: CycleRecord | None = None,
    ) -> None:
        other_worker: asyncio.Task[None] | None = None
        async with self._lock:
            self._failure = str(failure) or type(failure).__name__
            self._disconnected = (
                self._is_disconnect(failure) if disconnected is None else disconnected
            )
            self._phase = RunnerPhase.STOPPED
            self._generation += 1
            current = asyncio.current_task()
            if (
                self._worker is not None
                and self._worker is not current
                and not self._worker.done()
            ):
                other_worker = self._worker
                self._cancel_reasons[other_worker] = "callback_failure"
                other_worker.cancel()
        try:
            if record is not None:
                await self._deliver_record(record)
        except _CycleCallbackError as exc:
            self._failure = str(exc)
        finally:
            await self._wait_worker(other_worker)
            self._stopped.set()

    def _record_from_attempt(
        self,
        attempt: _CycleAttempt,
        *,
        completed: bool,
        interruption_reason: str | None,
    ) -> CycleRecord:
        actual_gap_ms = 0
        if attempt.gap_started_ms is not None:
            actual_gap_ms = max(0, self._now_ms() - attempt.gap_started_ms)
            actual_gap_ms = min(actual_gap_ms, attempt.planned_gap_ms)
        return CycleRecord(
            channel=self.channel,
            cycle_index=attempt.cycle_index,
            plot_event_id=attempt.directive.plot_event_id,
            pattern=attempt.directive.pattern,
            waveform_hash=attempt.waveform_hash,
            requested_strength=attempt.directive.requested_strength,
            effective_strength=attempt.effective_strength,
            active_start_offset_ms=attempt.active_start_offset_ms,
            raw_duration_ms=attempt.raw_duration_ms,
            gap_tenths=attempt.gap_tenths,
            planned_gap_ms=attempt.planned_gap_ms,
            actual_gap_ms=actual_gap_ms,
            completed=completed,
            interruption_reason=interruption_reason,
        )

    async def _deliver_record(self, record: CycleRecord) -> None:
        await self._start_record_delivery(record)

    async def _start_record_delivery(self, record: CycleRecord | None = None) -> None:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("cycle record delivery requires an asyncio task")

        async with self._delivery_lock:
            if record is not None:
                key = (record.channel, record.cycle_index)
                self._pending_records[key] = record
            if self._delivery_owner is not None:
                # The active owner drains the shared outbox after its callback.
                return
            if not self._pending_records:
                return
            self._delivery_owner = current

        try:
            while True:
                async with self._delivery_lock:
                    if not self._pending_records:
                        self._delivery_owner = None
                        return
                    pending_key, pending = next(iter(self._pending_records.items()))
                cancelled_after_delivery = await self._invoke_callback(pending)
                async with self._delivery_lock:
                    if self._pending_records.get(pending_key) is pending:
                        self._pending_records.pop(pending_key, None)
                if cancelled_after_delivery:
                    raise asyncio.CancelledError
        finally:
            async with self._delivery_lock:
                if self._delivery_owner is current:
                    self._delivery_owner = None

    async def _invoke_callback(self, record: CycleRecord) -> bool:
        try:
            result = self._on_cycle(record)
        except asyncio.CancelledError as exc:
            raise _CycleCallbackError("cycle callback cancelled") from exc
        except Exception as exc:
            raise _CycleCallbackError(f"cycle callback failed: {exc}") from exc
        if inspect.isawaitable(result):
            return await self._await_callback(result)
        return False

    async def _await_callback(self, result: Any) -> bool:
        delivery = asyncio.ensure_future(result)
        try:
            await asyncio.shield(delivery)
            return False
        except asyncio.CancelledError as exc:
            if asyncio.current_task().cancelling():
                try:
                    await delivery
                except asyncio.CancelledError as callback_exc:
                    raise _CycleCallbackError("cycle callback cancelled") from callback_exc
                except Exception as callback_exc:
                    raise _CycleCallbackError(
                        f"cycle callback failed: {callback_exc}"
                    ) from callback_exc
                return True
            raise _CycleCallbackError("cycle callback cancelled") from exc
        except Exception as exc:
            raise _CycleCallbackError(f"cycle callback failed: {exc}") from exc

    def _worker_finished(
        self, worker: asyncio.Task[None], startup: asyncio.Future[None]
    ) -> None:
        self._resolve_startup(startup)
        self._cancel_reasons.pop(worker, None)
        if self._worker is worker:
            self._worker = None
        if not worker.cancelled():
            worker.exception()

    def _stop_finished(self, stop_task: asyncio.Task[None]) -> None:
        if self._stop_task is stop_task:
            self._stop_task = None
            self._stop_clear_task = None
            self._stop_invalidation = None
        if not stop_task.cancelled():
            stop_task.exception()

    @staticmethod
    def _resolve_startup(startup: asyncio.Future[None]) -> None:
        if not startup.done():
            startup.set_result(None)

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
    def _read_effective_strength(
        executed: object, expected_action: Mapping[str, Any]
    ) -> int:
        if isinstance(executed, Sequence) and not isinstance(executed, (str, bytes)):
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
                    value = effective.get("effective_strength")
                    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 200:
                        return value
        raise ValueError("executor result requires effective_strength in 0..200")

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
