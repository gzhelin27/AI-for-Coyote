"""Controllable timeline test doubles shared by timeline test suites."""

from __future__ import annotations

import asyncio
import random
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence
from unittest.mock import patch

from backend.timeline.cycle_runner import ChannelCycleRunner
from backend.timeline.models import (
    SCHEMA_VERSION,
    ChannelDirective,
    CycleGapPolicy,
    CycleRecord,
    DirectiveMode,
    PlotEvent,
    ReplayManifest,
    SessionStatus,
    Timeline,
)
from backend.timeline.player import RecordedCyclePlayer
from backend.timeline.replay_store import ReplayStore, ReplaySummary


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

    def feed(self, gap_tenths: Sequence[int]) -> None:
        self._values.extend(gap_tenths)

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


class AdvancingSleeper:
    """Advance monotonic test time instead of waiting on wall time."""

    def __init__(self) -> None:
        self.now_ms = 0
        self.sleep_calls: list[int] = []

    async def sleep(self, ms: int) -> None:
        if ms <= 0:
            raise AssertionError("advancing sleeps must have a positive duration")
        self.sleep_calls.append(ms)
        self.now_ms += ms
        await asyncio.sleep(0)


class FakeTimelineGameLoop:
    """GameLoop-shaped executor that preserves authoritative result structure."""

    def __init__(
        self,
        *,
        frames: Mapping[str, Sequence[str]],
        caps: Mapping[str, int] | None = None,
        fail_on_cycle: int | None = None,
        disconnect_on_cycle: int | None = None,
        decoy_strength_result: bool = False,
    ) -> None:
        self.frames = {name: tuple(values) for name, values in frames.items()}
        self.caps = {"A": 200, "B": 200, **dict(caps or {})}
        self.fail_on_cycle = fail_on_cycle
        self.disconnect_on_cycle = disconnect_on_cycle
        self.decoy_strength_result = decoy_strength_result
        self.execute_calls: list[list[dict[str, Any]]] = []
        self.requested_cycle_actions: list[dict[str, Any]] = []
        self.cycle_start_frames: list[int] = []
        self.clear_calls: list[str | None] = []
        self.operation_log: list[str] = []
        self.clear_failures_remaining = 0
        self.block_channel_clear = False
        self.clear_started = asyncio.Event()
        self.release_clear = asyncio.Event()
        self._cycle_attempts = 0
        self.safety = SimpleNamespace(
            current={"A": 0, "B": 0},
            enabled={"A": True, "B": True},
            presets={
                name: {"frames": list(values)} for name, values in self.frames.items()
            },
            cap_for=lambda channel: self.caps[channel],
            estop_active=False,
        )
        self.autopilot_interval = 12.0

    async def execute_actions(
        self, actions: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        self.execute_calls.append([dict(action) for action in actions])
        executed: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        for action in actions:
            op = action.get("op")
            channel = action.get("channel")
            if op == "hold_strength":
                requested = action["value"]
                effective_strength = min(requested, self.caps[channel])
                self.safety.current[channel] = effective_strength
                if self.decoy_strength_result:
                    executed.append(
                        {
                            "action": dict(action),
                            "effective": {
                                "op": op,
                                "channel": "B" if channel == "A" else "A",
                                "requested_strength": requested,
                                "effective_strength": requested,
                            },
                        }
                    )
                executed.append(
                    {
                        "action": dict(action),
                        "effective": {
                            "op": op,
                            "channel": channel,
                            "requested_strength": requested,
                            "effective_strength": effective_strength,
                        },
                    }
                )
                continue
            if op == "pulse_cycle":
                self._cycle_attempts += 1
                if self.fail_on_cycle == self._cycle_attempts:
                    raise RuntimeError("injected replay executor failure")
                if self.disconnect_on_cycle == self._cycle_attempts:
                    dropped.append(
                        {"action": dict(action), "reason": "device disconnected"}
                    )
                    continue
                self.operation_log.append(f"cycle:{channel}")
                requested = dict(action)
                self.requested_cycle_actions.append(requested)
                self.cycle_start_frames.append(0)
                executed.append(
                    {
                        "action": requested,
                        "effective": {
                            "op": op,
                            "channel": channel,
                            "pattern": action["pattern"],
                            "effective_strength": self.safety.current[channel],
                            "duration_ms": len(self.frames[action["pattern"]]) * 100,
                        },
                    }
                )
                continue
            if op == "clear":
                if channel is None:
                    self.safety.current = {"A": 0, "B": 0}
                else:
                    self.safety.current[channel] = 0
                executed.append({"action": dict(action), "effective": dict(action)})
                continue
            if op == "stop":
                self.safety.current = {"A": 0, "B": 0}
                executed.append({"action": dict(action), "effective": dict(action)})
                continue
            dropped.append({"action": dict(action), "reason": "unsupported action"})
        return executed, dropped

    async def clear_output(
        self, channel: str | None = None
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        self.clear_calls.append(channel)
        self.operation_log.append(f"clear:{channel or '*'}")
        if channel is not None and self.block_channel_clear:
            self.clear_started.set()
            await self.release_clear.wait()
        if self.clear_failures_remaining:
            self.clear_failures_remaining -= 1
            raise RuntimeError("injected clear failure")
        action = {"op": "stop"} if channel is None else {"op": "clear", "channel": channel}
        return await self.execute_actions([action])


class FakeTimelineResolver:
    """Resolve literal set/keep/stop intent without sampling plot randomness."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def resolve_plot_event(self, **values: Any) -> PlotEvent:
        self.calls.append(dict(values))
        channels: dict[str, ChannelDirective] = {}
        for channel in ("A", "B"):
            if not values["enabled"].get(channel, False):
                continue
            matching = [
                action
                for action in values["actions"]
                if action.get("channel") == channel
                or (action.get("op") == "stop" and action.get("channel") is None)
            ]
            action = matching[-1] if matching else None
            if action is None:
                channels[channel] = ChannelDirective(channel, DirectiveMode.KEEP)
            elif action.get("op") in ("clear", "stop"):
                channels[channel] = ChannelDirective(channel, DirectiveMode.STOP)
            else:
                strength = int(action.get("value", values["current"].get(channel, 0)))
                channels[channel] = ChannelDirective(
                    channel=channel,
                    mode=DirectiveMode.SET,
                    pattern=values["presets"][0],
                    base_strength=strength,
                    resolved_strength=strength,
                )
        return PlotEvent(
            event_id=values["event_id"],
            scene_id=values["scene_id"],
            offset_ms=values["offset_ms"],
            channels=channels,
        )


class SessionHarness:
    """Compose the real controller/runners with controlled active time."""

    def __init__(self, seed: int) -> None:
        from backend.timeline.session import SessionController

        self._temporary = tempfile.TemporaryDirectory()
        self.clock = ControlledSleeper()
        self.game_loop = FakeTimelineGameLoop(frames={"呼吸": ("f0", "f1")})
        self.store = ReplayStore(Path(self._temporary.name))
        self.resolver = FakeTimelineResolver()
        self.gap_rngs = {
            "A": SequenceGapRandom(()),
            "B": SequenceGapRandom(()),
        }
        self.controller = SessionController(
            game_loop=self.game_loop,
            store=self.store,
            seed=seed,
            frames=self.game_loop.frames,
            clock=lambda: self.clock.now_ms / 1000,
            sleeper=self.clock,
            resolver_factory=lambda _seed: self.resolver,
            cycle_rngs=self.gap_rngs,
            session_id_factory=lambda: f"session-{seed}",
            replay_id_factory=lambda: f"replay-{seed}",
            timestamp_factory=lambda: "2026-08-31T00:00:00+00:00",
        )

    @classmethod
    def create(cls, seed: int) -> "SessionHarness":
        return cls(seed)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.controller, name)

    @property
    def clear_calls(self) -> list[str | None]:
        return self.game_loop.clear_calls

    @property
    def last_cycle_started_at_frame(self) -> int | None:
        if not self.game_loop.cycle_start_frames:
            return None
        return self.game_loop.cycle_start_frames[-1]

    async def _ensure_event(self, channel: str) -> None:
        if not self.controller.to_state().current_event_id:
            await self.controller.process_live_turn(
                [{"op": "hold_strength", "channel": channel, "value": 20}]
            )

    async def complete_cycles(
        self, channel: str, *, gap_tenths: Sequence[int]
    ) -> None:
        await self._ensure_event(channel)
        self.gap_rngs[channel].feed(gap_tenths)
        initial = len(self.controller.recorded_cycles)
        target = initial + len(gap_tenths)
        for _ in range(max(1, len(gap_tenths)) * 40):
            if len(self.controller.recorded_cycles) >= target:
                return
            remaining = self.clock.next_remaining_ms
            if remaining is not None:
                self.clock.advance(remaining)
            await asyncio.sleep(0)
        raise AssertionError("session cycles did not complete")

    async def complete_next_cycle(self, channel: str) -> None:
        await self._ensure_event(channel)
        initial = sum(
            record.channel == channel and record.completed
            for record in self.controller.recorded_cycles
        )
        for _ in range(40):
            completed = sum(
                record.channel == channel and record.completed
                for record in self.controller.recorded_cycles
            )
            if completed > initial:
                return
            remaining = self.clock.next_remaining_ms
            if remaining is not None:
                self.clock.advance(remaining)
            await asyncio.sleep(0)
        raise AssertionError("next session cycle did not complete")

    async def begin_partial_cycle(self, channel: str) -> None:
        await self._ensure_event(channel)
        await asyncio.sleep(0)

    def redeliver_latest_cycle(self, channel: str) -> None:
        record = next(
            record
            for record in reversed(self.controller.recorded_cycles)
            if record.channel == channel
        )
        self.controller.runners[channel]._on_cycle(record)

    async def pause(self, *, manual_elapsed_ms: int = 0) -> Any:
        state = await self.controller.pause()
        if manual_elapsed_ms:
            self.clock.advance(manual_elapsed_ms)
        return state

    async def close(self) -> None:
        await self.controller.stop()
        self._temporary.cleanup()


class _CountingCycleRandom:
    """Count policy samples while retaining the real seeded Random behavior."""

    def __init__(self, seed: int) -> None:
        self._random = random.Random(seed)
        self.calls = 0

    def randrange(self, *args: Any) -> int:
        self.calls += 1
        return self._random.randrange(*args)

    def randint(self, start: int, stop: int) -> int:
        return self._random.randint(start, stop)


class _TimelineRelay:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []
        self._strength_failures: list[str] = []
        self._clear_failures: list[str | None] = []
        self.fail_all_clears = False

    def first_client_id(self) -> str:
        return "integration-client"

    def get_slot_id(self, _client_id: str | None = None) -> str:
        return "integration-slot"

    async def send_frame(self, frame: dict[str, Any]) -> bool:
        self.attempts.append(frame)
        method, payload = self._operation(frame)
        channel = self._channel_name(payload)
        if method == "device.op" and payload.get("t") == 3:
            if channel in self._strength_failures:
                self._strength_failures.remove(channel)
                return False
        if method == "device.op.clear":
            if self.fail_all_clears or channel in self._clear_failures:
                if channel in self._clear_failures:
                    self._clear_failures.remove(channel)
                return False
        self.frames.append(frame)
        return True

    def fail_next_strength_delta(self, channel: str) -> None:
        if channel not in ("A", "B"):
            raise ValueError("channel must be A or B")
        self._strength_failures.append(channel)

    def fail_next_clear(self, channel: str | None = None) -> None:
        if channel not in (None, "A", "B"):
            raise ValueError("channel must be A, B, or None")
        self._clear_failures.append(channel)

    def strength_deltas(self, channel: str) -> list[int]:
        if channel not in ("A", "B"):
            raise ValueError("channel must be A or B")
        return [
            int(payload["v"])
            for frame in self.attempts
            for method, payload in (self._operation(frame),)
            if method == "device.op"
            and self._channel_name(payload) == channel
            and payload.get("t") == 3
        ]

    @classmethod
    def is_positive_manual_output(cls, frame: dict[str, Any]) -> bool:
        method, payload = cls._operation(frame)
        value = payload.get("v")
        return (
            method == "device.op"
            and payload.get("t") == 3
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value > 0
        )

    @classmethod
    def is_waveform_helper(cls, frame: dict[str, Any]) -> bool:
        method, payload = cls._operation(frame)
        values = payload.get("v")
        return (
            method == "device.op"
            and payload.get("t") == 0
            and isinstance(values, list)
            and len(values) > 2
        )

    @staticmethod
    def _operation(frame: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        data = frame.get("data") if isinstance(frame, dict) else None
        payload = data.get("data") if isinstance(data, dict) else None
        method = data.get("m") if isinstance(data, dict) else None
        return method, payload if isinstance(payload, dict) else {}

    @staticmethod
    def _channel_name(payload: Mapping[str, Any]) -> str | None:
        return {0: "A", 1: "B"}.get(payload.get("c"))

    def to_state(self) -> dict[str, Any]:
        return {
            "status": "paired",
            "controller_id": "integration-controller",
            "url": "ws://integration.invalid",
            "clients": [],
            "last_error": "",
        }


class TimelineHarness:
    """Real MVP1 composition with only time, relay, and storage controlled."""

    def __init__(self) -> None:
        raise RuntimeError("use TimelineHarness.create")

    @classmethod
    async def create(
        cls,
        seed: int,
        dry_run: bool,
        cycle_rngs: Mapping[str, Any] | None = None,
    ) -> "TimelineHarness":
        from backend.config import DEFAULTS
        from backend.game_loop import GameLoop
        from backend.safety import SafetyManager
        from backend.timeline.models import CycleGapPolicy
        from backend.timeline.randomizer import derive_stream_seed
        from backend.timeline.session import SessionController

        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("seed must be an integer")
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")

        self = object.__new__(cls)
        self._temporary = tempfile.TemporaryDirectory()
        self.clock = ControlledSleeper()
        self.relay = _TimelineRelay()
        cfg = deepcopy(DEFAULTS)
        cfg["app"]["dry_run"] = dry_run
        cfg["autopilot"] = {"enabled": False, "interval_s": 12}
        cfg["character"] = {
            "name": "MVP1 Integration",
            "role": "integration",
            "role_title": "operator",
            "roles": [],
            "profile": "dry-run",
            "profiles": ["dry-run"],
            "profile_available": {"dry-run": True},
            "profile_level": "test",
            "rage_baseline": 0,
            "player_nick": "tester",
        }
        frames = {
            "呼吸": ("integration-frame-0", "integration-frame-1"),
            "潮汐": ("integration-frame-2",),
        }
        cfg["presets"] = {
            name: {
                "waveform": f"integration-{index}",
                "label": name,
                "category": "integration",
                "frames": list(values),
                "default_duration_s": 1,
                "max_duration_s": 10,
            }
            for index, (name, values) in enumerate(frames.items())
        }

        async def unexpected_chat(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("integration harness must not call an LLM")

        self.safety = SafetyManager(cfg)
        self.loop = GameLoop(
            cfg,
            SimpleNamespace(chat=unexpected_chat),
            self.safety,
            self.relay,
        )
        self.store = ReplayStore(Path(self._temporary.name))
        if cycle_rngs is None:
            self._cycle_rngs = {
                channel: _CountingCycleRandom(
                    derive_stream_seed(seed, f"cycle:{channel}")
                )
                for channel in ("A", "B")
            }
        else:
            if set(cycle_rngs) - {"A", "B"}:
                raise ValueError("cycle RNGs may only be provided for A or B")
            self._cycle_rngs = {
                channel: cycle_rngs.get(
                    channel,
                    _CountingCycleRandom(derive_stream_seed(seed, f"cycle:{channel}")),
                )
                for channel in ("A", "B")
            }
        self.controller = SessionController(
            game_loop=self.loop,
            store=self.store,
            seed=seed,
            frames=frames,
            strength_jitter=4,
            waveform_policy="all_allowed",
            cycle_gap_policy=CycleGapPolicy(),
            clock=lambda: self.clock.now_ms / 1000,
            sleeper=self.clock,
            cycle_rngs=self._cycle_rngs,
            session_id_factory=lambda: f"integration-session-{seed}",
            replay_id_factory=lambda: f"integration-replay-{seed}",
            timestamp_factory=lambda: "2026-08-31T00:00:00+00:00",
            manifest_metadata={
                "app_commit": "integration-build",
                "model": "no-llm",
                "dlc_role": "integration",
                "dlc_profile": "dry-run",
                "dlc_version": "integration-v1",
                "app_fingerprint": "integration-app-v1",
                "dlc_fingerprint": "integration-dlc-v1",
            },
        )
        self.loop.timeline_session = self.controller
        return self

    @property
    def rng_calls(self) -> int:
        return sum(rng.calls for rng in self._cycle_rngs.values())

    async def start(self) -> Any:
        return await self.controller.start_live()

    async def turn(self, actions: Sequence[Mapping[str, Any]]) -> Any:
        return await self.controller.process_live_turn(actions)

    async def complete_cycles(self, channel: str, *, count: int) -> None:
        if channel not in ("A", "B"):
            raise ValueError("channel must be A or B")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("count must be a non-negative integer")
        initial = sum(
            record.channel == channel and record.completed
            for record in self.controller.recorded_cycles
        )
        target = initial + count
        for _ in range(max(1, count) * 100):
            completed = sum(
                record.channel == channel and record.completed
                for record in self.controller.recorded_cycles
            )
            if completed >= target:
                return
            remaining = self.clock.next_remaining_ms
            if remaining is not None:
                self.clock.advance(remaining)
            await asyncio.sleep(0)
        raise AssertionError(f"{channel} did not complete {count} cycles")

    def completed_cycles(self, channel: str) -> int:
        if channel not in ("A", "B"):
            raise ValueError("channel must be A or B")
        return sum(
            record.channel == channel and record.completed
            for record in self.controller.recorded_cycles
        )

    async def wait_for_phase(self, channel: str, phase: str) -> None:
        if channel not in ("A", "B"):
            raise ValueError("channel must be A or B")
        for _ in range(100):
            runner = self.controller.runners.get(channel)
            if runner is not None and runner.state().phase.value == phase:
                return
            remaining = self.clock.next_remaining_ms
            if remaining is not None:
                self.clock.advance(remaining)
            await asyncio.sleep(0)
        raise AssertionError(f"{channel} did not enter {phase}")

    async def wait_for_pending_record(self, channel: str) -> None:
        if channel not in ("A", "B"):
            raise ValueError("channel must be A or B")
        for _ in range(100):
            runner = self.controller.runners.get(channel)
            if runner is not None and runner.pending_records():
                return
            remaining = self.clock.next_remaining_ms
            if remaining is not None:
                self.clock.advance(remaining)
            await asyncio.sleep(0)
        raise AssertionError(f"{channel} did not retain a pending record")

    def set_current_provenance(self, **metadata: str) -> None:
        allowed = {"app_fingerprint", "dlc_fingerprint"}
        if set(metadata) - allowed or not all(
            isinstance(value, str) and value for value in metadata.values()
        ):
            raise ValueError("only non-empty replay provenance fingerprints are allowed")
        self.controller._manifest_metadata.update(metadata)

    async def finish(self) -> ReplaySummary:
        return await self.controller.finish()

    async def replay(self, replay: Any) -> SimpleNamespace:
        before = self.rng_calls
        await self.controller.start_replay(replay.manifest.replay_id)
        player = self.controller.player
        if player is None:
            raise AssertionError("replay player was not installed")
        requested_cycles = player.ordered_cycles
        for _ in range(max(1, len(requested_cycles)) * 100):
            if self.controller.to_state().status is SessionStatus.IDLE:
                break
            remaining = self.clock.next_remaining_ms
            if remaining is not None:
                self.clock.advance(remaining)
            await asyncio.sleep(0)
        else:
            raise AssertionError("recorded replay did not finish")
        return SimpleNamespace(
            requested_cycles=requested_cycles,
            rng_calls=self.rng_calls - before,
        )

    async def close(self) -> None:
        await self.controller.stop()
        self._temporary.cleanup()


class ReplayHarness:
    """Compose exact playback with a GameLoop-shaped recording executor."""

    def __init__(
        self,
        cycles: Sequence[CycleRecord],
        *,
        frames: Mapping[str, Sequence[str]] | None = None,
        caps: Mapping[str, int] | None = None,
        controlled: bool = False,
        fail_on_cycle: int | None = None,
        decoy_strength_result: bool = False,
    ) -> None:
        self.bundle = SimpleNamespace(
            manifest=ReplayManifest(
                schema_version=SCHEMA_VERSION,
                replay_id="replay-player",
                session_id="session-player",
                seed=71,
                status=SessionStatus.COMPLETED,
                mode="autopilot",
            ),
            timeline=Timeline(
                schema_version=SCHEMA_VERSION,
                session_id="session-player",
                seed=71,
                plot_events=(),
                cycles=tuple(cycles),
            ),
        )
        self.sleeper = ControlledSleeper() if controlled else AdvancingSleeper()
        self.executor = FakeTimelineGameLoop(
            frames=frames or {"呼吸": ("f0", "f1")},
            caps=caps,
            fail_on_cycle=fail_on_cycle,
            decoy_strength_result=decoy_strength_result,
        )
        self.player = RecordedCyclePlayer(
            executor=self.executor,
            clock=lambda: self.sleeper.now_ms / 1000,
            sleeper=self.sleeper,
        )
        self.player.load(self.bundle)
        self.rng_calls = 0

        def counted_random(*_args: Any, **_kwargs: Any) -> SequenceGapRandom:
            self.rng_calls += 1
            return SequenceGapRandom(())

        self._random_patch = patch("random.Random", side_effect=counted_random)
        self._random_patch.start()

    @staticmethod
    def _cycle(
        *,
        channel: str,
        cycle_index: int,
        offset_ms: int,
        gap_tenths: int,
        requested_strength: int = 20,
        effective_strength: int = 20,
    ) -> CycleRecord:
        return CycleRecord(
            channel=channel,
            cycle_index=cycle_index,
            plot_event_id="evt-1",
            pattern="呼吸",
            waveform_hash="936b1d3d04551d6c7755f3850bc453c7245f048ff1824cb8ce3ecf710a0fa2c2",
            requested_strength=requested_strength,
            effective_strength=effective_strength,
            active_start_offset_ms=offset_ms,
            raw_duration_ms=200,
            gap_tenths=gap_tenths,
            planned_gap_ms=200 * gap_tenths // 10,
            actual_gap_ms=200 * gap_tenths // 10,
            completed=True,
            interruption_reason=None,
        )

    @classmethod
    def from_cycles(
        cls,
        *,
        gap_tenths: Sequence[int],
        controlled: bool = False,
        fail_on_cycle: int | None = None,
    ) -> "ReplayHarness":
        cycles: list[CycleRecord] = []
        offset_ms = 0
        for index, gap in enumerate(gap_tenths, 1):
            cycles.append(
                cls._cycle(
                    channel="A",
                    cycle_index=index,
                    offset_ms=offset_ms,
                    gap_tenths=gap,
                )
            )
            offset_ms += 200 + 200 * gap // 10
        return cls(
            cycles,
            controlled=controlled,
            fail_on_cycle=fail_on_cycle,
        )

    @classmethod
    def from_strength(
        cls,
        *,
        original: int,
        current_cap: int,
        decoy_strength_result: bool = False,
    ) -> "ReplayHarness":
        return cls(
            [
                cls._cycle(
                    channel="A",
                    cycle_index=1,
                    offset_ms=0,
                    gap_tenths=0,
                    requested_strength=original,
                    effective_strength=original,
                )
            ],
            caps={"A": current_cap},
            decoy_strength_result=decoy_strength_result,
        )

    @classmethod
    def from_channel_cycles(
        cls, cycles: Sequence[tuple[str, int, int]]
    ) -> "ReplayHarness":
        per_channel_index = {"A": 0, "B": 0}
        records = []
        for channel, offset_ms, gap_tenths in cycles:
            per_channel_index[channel] += 1
            records.append(
                cls._cycle(
                    channel=channel,
                    cycle_index=per_channel_index[channel],
                    offset_ms=offset_ms,
                    gap_tenths=gap_tenths,
                )
            )
        return cls(records)

    @property
    def requested_gap_tenths(self) -> list[int]:
        count = len(self.executor.requested_cycle_actions)
        played = self.player.ordered_cycles[:count]
        return [cycle.gap_tenths for cycle in played]

    async def close(self) -> None:
        try:
            await self.player.stop()
        finally:
            self._random_patch.stop()
