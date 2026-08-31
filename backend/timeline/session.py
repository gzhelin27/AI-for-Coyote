"""Lifecycle orchestration for live deterministic sessions and exact replay."""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from .cycle_runner import ChannelCycleRunner, CycleDirective, RunnerPhase
from .models import (
    SCHEMA_VERSION,
    ChannelPlaybackState,
    CycleGapPolicy,
    CycleRecord,
    DirectiveMode,
    PlotEvent,
    ReplayManifest,
    SessionState,
    SessionStatus,
    Timeline,
)
from .player import RecordedCyclePlayer
from .randomizer import TimelineResolver, derive_stream_seed
from .replay_store import ReplayStore, ReplaySummary


_CHANNELS = ("A", "B")


class _AsyncioSleeper:
    async def sleep(self, ms: int) -> None:
        await asyncio.sleep(ms / 1000)


class _RunnerExecutor:
    """Adapt the GameLoop executor name without changing its result contract."""

    def __init__(self, game_loop: Any) -> None:
        self._game_loop = game_loop
        self._owner_generations: dict[str, int] = {}

    def update_owner_generations(
        self, generations: Mapping[str, int]
    ) -> None:
        self._owner_generations.update(
            {str(channel): int(value) for channel, value in generations.items()}
        )

    def clear_owner_generations(self) -> None:
        self._owner_generations.clear()

    async def execute(
        self, actions: list[dict[str, Any]]
    ) -> tuple[list[Any], list[Any]]:
        execute_timeline = getattr(
            self._game_loop, "execute_timeline_actions", None
        )
        if callable(execute_timeline):
            return await execute_timeline(
                actions, dict(self._owner_generations)
            )
        return await self._game_loop.execute_actions(actions)


class SessionController:
    """Own one live recording session or one recorded replay at a time."""

    def __init__(
        self,
        *,
        game_loop: Any,
        store: ReplayStore,
        seed: int | None = None,
        seed_factory: Callable[[], int] | None = None,
        frames: Mapping[str, Sequence[str]] | None = None,
        strength_jitter: int = 4,
        waveform_policy: str = "all_allowed",
        cycle_gap_policy: CycleGapPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Any | None = None,
        resolver_factory: Callable[[int], Any] | None = None,
        cycle_rngs: Mapping[str, Any] | None = None,
        session_id_factory: Callable[[], str] | None = None,
        replay_id_factory: Callable[[], str] | None = None,
        timestamp_factory: Callable[[], str] | None = None,
        manifest_metadata: Mapping[str, Any] | None = None,
        manifest_metadata_factory: Callable[[], Mapping[str, Any]] | None = None,
        player_factory: Callable[..., RecordedCyclePlayer] = RecordedCyclePlayer,
    ) -> None:
        if not hasattr(game_loop, "execute_actions") or not hasattr(
            game_loop, "clear_output"
        ):
            raise TypeError("game_loop must provide execute_actions and clear_output")
        if not isinstance(store, ReplayStore):
            raise TypeError("store must be a ReplayStore")
        if seed is None and seed_factory is None:
            raise ValueError("seed or seed_factory is required")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            raise ValueError("seed must be an integer")
        if seed_factory is not None and not callable(seed_factory):
            raise TypeError("seed_factory must be callable")
        if manifest_metadata_factory is not None and not callable(
            manifest_metadata_factory
        ):
            raise TypeError("manifest_metadata_factory must be callable")
        if (
            isinstance(strength_jitter, bool)
            or not isinstance(strength_jitter, int)
            or strength_jitter < 0
        ):
            raise ValueError("strength_jitter must be a non-negative integer")
        if waveform_policy != "all_allowed":
            raise ValueError("waveform_policy must be all_allowed for MVP1")
        if not callable(clock):
            raise TypeError("clock must be callable")
        sleeper = _AsyncioSleeper() if sleeper is None else sleeper
        if not hasattr(sleeper, "sleep"):
            raise TypeError("sleeper must provide sleep(ms)")

        self.game_loop = game_loop
        self.store = store
        self.seed = 0 if seed is None else seed
        self._seed_factory = seed_factory
        self.strength_jitter = strength_jitter
        self.waveform_policy = waveform_policy
        self.policy = cycle_gap_policy or CycleGapPolicy()
        self._clock = clock
        self._sleeper = sleeper
        self._resolver_factory = resolver_factory or (
            lambda session_seed: TimelineResolver(
                strength_jitter=self.strength_jitter,
                session_seed=session_seed,
            )
        )
        self._provided_cycle_rngs = dict(cycle_rngs) if cycle_rngs is not None else None
        if self._provided_cycle_rngs is not None and set(self._provided_cycle_rngs) != set(
            _CHANNELS
        ):
            raise ValueError("cycle_rngs must provide A and B")
        self._session_id_factory = session_id_factory or (lambda: str(uuid.uuid4()))
        self._replay_id_factory = replay_id_factory or (lambda: str(uuid.uuid4()))
        self._timestamp_factory = timestamp_factory or (
            lambda: datetime.now(timezone.utc).isoformat()
        )
        self._manifest_metadata = dict(manifest_metadata or {})
        self._manifest_metadata_factory = manifest_metadata_factory
        self._player_factory = player_factory
        self._frames = self._normalize_frames(frames)
        self._runner_executor = _RunnerExecutor(game_loop)

        self._lock = asyncio.Lock()
        self._turn_lock = asyncio.Lock()
        self._routing_generation = 0
        self._status = SessionStatus.IDLE
        self._mode: str | None = None
        self._session_id: str | None = None
        self._replay_id: str | None = None
        self._resolver: Any | None = None
        self._cycle_rngs: dict[str, Any] = {}
        self._runners: dict[str, ChannelCycleRunner] = {}
        self._runner_watchers: dict[str, asyncio.Task[None]] = {}
        self._player: RecordedCyclePlayer | None = None
        self._player_watcher: asyncio.Task[None] | None = None
        self._plot_events: list[PlotEvent] = []
        self._cycle_records: dict[tuple[str, int], CycleRecord] = {}
        self._retained: dict[str, CycleDirective] = {}
        self._event_cursor = 0
        self._current_event_id: str | None = None
        self._created_at = ""
        self._active_started_at: float | None = None
        self._pause_started_at: float | None = None
        self._paused_total_ms = 0
        self._safety_caps: dict[str, int] = {}
        self._live_clear_required = False
        self._output_generations: dict[str, int] = {}

    @property
    def runners(self) -> Mapping[str, ChannelCycleRunner]:
        return dict(self._runners)

    @property
    def player(self) -> RecordedCyclePlayer | None:
        return self._player

    @property
    def recorded_cycles(self) -> tuple[CycleRecord, ...]:
        return tuple(self._cycle_records.values())

    @property
    def routing_generation(self) -> int:
        return self._routing_generation

    async def start_live(self) -> SessionState:
        async with self._lock:
            self._require_estop_inactive()
            if self._status is SessionStatus.PAUSED and self._mode == "autopilot":
                await self._resume_live_locked()
                return self.to_state()
            if self._status is not SessionStatus.IDLE:
                raise RuntimeError("a timeline session is already active")

            if self._seed_factory is not None:
                next_seed = self._seed_factory()
                if isinstance(next_seed, bool) or not isinstance(next_seed, int):
                    raise ValueError("seed_factory must return an integer")
                self.seed = next_seed
            if self._manifest_metadata_factory is not None:
                metadata = self._manifest_metadata_factory()
                if not isinstance(metadata, Mapping):
                    raise TypeError(
                        "manifest_metadata_factory must return a mapping"
                    )
                self._manifest_metadata = dict(metadata)

            self._set_status(SessionStatus.RUNNING)
            self._mode = "autopilot"
            self._session_id = self._require_id(
                self._session_id_factory(), "session ID"
            )
            self._replay_id = None
            self._resolver = self._resolver_factory(self.seed)
            if not hasattr(self._resolver, "resolve_plot_event"):
                raise TypeError("resolver must provide resolve_plot_event")
            self._cycle_rngs = self._new_cycle_rngs()
            self._plot_events = []
            self._cycle_records = {}
            self._retained = {}
            self._event_cursor = 0
            self._current_event_id = None
            self._created_at = self._timestamp_factory()
            self._active_started_at = self._clock()
            self._pause_started_at = None
            self._paused_total_ms = 0
            self._safety_caps = self._current_caps()
            self._live_clear_required = False
            self._runners = {}
            for channel in _CHANNELS:
                self._install_runner(channel)
            return self.to_state()

    async def process_live_turn(
        self,
        actions: Sequence[Mapping[str, Any]],
        *,
        scene_id: str | None = None,
        expected_routing_generation: int | None = None,
    ) -> PlotEvent | None:
        async with self._turn_lock:
            submissions: list[
                tuple[str, ChannelCycleRunner, CycleDirective]
            ] = []
            async with self._lock:
                if (
                    expected_routing_generation is not None
                    and self._routing_generation != expected_routing_generation
                ):
                    return None
                if (
                    self._status is not SessionStatus.RUNNING
                    or self._mode != "autopilot"
                ):
                    raise RuntimeError("live session is not running")
                if self._resolver is None:
                    raise RuntimeError("live resolver is unavailable")

                self._event_cursor += 1
                event_id = f"evt-{self._event_cursor:06d}"
                resolved = self._resolver.resolve_plot_event(
                    actions=actions,
                    current=self._current_strengths(),
                    caps=self._current_caps(),
                    enabled=self._enabled_channels(),
                    presets=tuple(self._frames),
                    event_id=event_id,
                    scene_id=scene_id or f"live-turn-{self._event_cursor}",
                    offset_ms=self._active_offset_ms(),
                )
                if not isinstance(resolved, PlotEvent):
                    raise TypeError("resolver must return a PlotEvent")
                self._plot_events.append(resolved)
                self._current_event_id = resolved.event_id

                requested_stops = tuple(
                    channel
                    for channel, directive in resolved.channels.items()
                    if directive.mode is DirectiveMode.STOP
                )
                if requested_stops:
                    self._require_output_clear(requested_stops)

                stopped_channels: list[str] = []
                for channel, directive in resolved.channels.items():
                    if directive.mode is DirectiveMode.KEEP:
                        continue
                    if directive.mode is DirectiveMode.STOP:
                        self._retained.pop(channel, None)
                        await self._retire_runner_locked(channel, "plot_stop")
                        stopped_channels.append(channel)
                        continue

                    cycle_directive = CycleDirective(
                        channel=channel,
                        plot_event_id=resolved.event_id,
                        pattern=directive.pattern or "",
                        requested_strength=(
                            directive.resolved_strength
                            if directive.resolved_strength is not None
                            else 0
                        ),
                    )
                    self._retained[channel] = cycle_directive
                    runner = self._runners.get(channel)
                    if runner is not None and runner.state().phase is RunnerPhase.STOPPED:
                        await self._settle_stopped_runner_locked(channel, runner)
                        if self._status is not SessionStatus.RUNNING:
                            continue
                        runner = self._runners.get(channel)
                    if runner is None:
                        runner = self._install_runner(channel)
                    submissions.append((channel, runner, cycle_directive))

                if stopped_channels:
                    if set(stopped_channels) == set(_CHANNELS):
                        result = await self.game_loop.clear_output()
                        self._require_clear_result(result)
                    else:
                        for channel in stopped_channels:
                            result = await self.game_loop.clear_output(channel)
                            self._require_clear_result(result, channel)

            for channel, runner, directive in submissions:
                async with self._lock:
                    if not self._submission_is_current(channel, runner, directive):
                        continue
                try:
                    await runner.submit(directive)
                except RuntimeError:
                    async with self._lock:
                        if not self._submission_is_current(
                            channel, runner, directive
                        ):
                            continue
                    raise
            return resolved

    async def suspend_channel_for_safety(
        self, channel: str, *, reason: str
    ) -> bool:
        """Quiesce one live runner before an authoritative safety transition.

        The return value says whether the retained directive may be restarted
        after the physical reduction/clear has succeeded.
        """
        if channel not in _CHANNELS:
            raise ValueError("channel must be A or B")
        if not isinstance(reason, str) or not reason:
            raise ValueError("reason must be a non-empty string")
        async with self._turn_lock:
            async with self._lock:
                if (
                    self._status is not SessionStatus.RUNNING
                    or self._mode != "autopilot"
                ):
                    return False
                if channel in self._runners:
                    await self._retire_runner_locked(channel, reason)
                return channel in self._retained

    async def resume_channel_after_safety(self, channel: str) -> None:
        """Restart a retained live directive only after safety is enforced."""
        if channel not in _CHANNELS:
            raise ValueError("channel must be A or B")
        async with self._turn_lock:
            async with self._lock:
                if (
                    self._status is not SessionStatus.RUNNING
                    or self._mode != "autopilot"
                    or not self._enabled_channels().get(channel, False)
                ):
                    return
                directive = self._retained.get(channel)
                if directive is None:
                    return
                runner = self._runners.get(channel)
                if runner is None:
                    runner = self._install_runner(channel)
                await runner.submit(directive)

    async def pause(self) -> SessionState:
        async with self._lock:
            if self._status is SessionStatus.PAUSED:
                if self._mode == "replay" and self._player is not None:
                    self._set_status(SessionStatus.FINISHING)
                    await self._player.pause()
                    self._set_status(SessionStatus.PAUSED)
                elif self._mode == "autopilot" and self._live_clear_required:
                    await self._pause_live_locked("operator_pause_retry")
                return self.to_state()
            if self._status is SessionStatus.FINISHING:
                if self._mode == "replay" and self._player is not None:
                    await self._player.pause()
                    self._set_status(SessionStatus.PAUSED)
                elif self._mode == "autopilot" and self._live_clear_required:
                    await self._pause_live_locked("operator_pause_retry")
                else:
                    raise RuntimeError("session cleanup is still pending")
                return self.to_state()
            if self._status is SessionStatus.REPLAYING:
                if self._player is None:
                    raise RuntimeError("replay player is unavailable")
                self._set_status(SessionStatus.FINISHING)
                await self._player.pause()
                self._set_status(SessionStatus.PAUSED)
                return self.to_state()
            if self._status is not SessionStatus.RUNNING or self._mode != "autopilot":
                raise RuntimeError("no running session to pause")
            await self._pause_live_locked("operator_pause")
            return self.to_state()

    async def resume(self, cursor: int | None = None) -> SessionState:
        async with self._lock:
            if self._mode == "autopilot" and cursor is not None:
                raise ValueError("live sessions do not accept a replay cursor")
            if self._status is SessionStatus.RUNNING and self._mode == "autopilot":
                return self.to_state()
            if self._status is SessionStatus.REPLAYING:
                if self._player is None:
                    raise RuntimeError("replay player is unavailable")
                if cursor is not None:
                    self._player.validate_cursor(cursor)
                return self.to_state()
            if self._status is SessionStatus.FINISHING:
                raise RuntimeError("output clear is still pending")
            if self._status is not SessionStatus.PAUSED:
                raise RuntimeError("no paused session to resume")
            self._require_estop_inactive()
            if self._mode == "replay":
                if self._player is None:
                    raise RuntimeError("replay player is unavailable")
                self._set_player_output_generations(self._player)
                await self._player.resume(cursor)
                self._set_status(SessionStatus.REPLAYING)
                self._start_player_watcher_locked(self._player)
            elif self._mode == "autopilot":
                await self._resume_live_locked()
            else:
                raise RuntimeError("paused session has no mode")
            return self.to_state()

    async def finish(self) -> ReplaySummary:
        async with self._lock:
            if self._mode != "autopilot" or self._status not in (
                SessionStatus.RUNNING,
                SessionStatus.PAUSED,
                SessionStatus.FINISHING,
            ):
                raise RuntimeError("no live session to finish")
            was_paused = self._status is SessionStatus.PAUSED
            self._set_status(SessionStatus.FINISHING)
            if not was_paused and self._pause_started_at is None:
                self._pause_started_at = self._clock()
            # A cancellation can arrive while watcher teardown is still pending.
            # Mark the clear before that first await so stop() can always retry it.
            self._live_clear_required = True
            self._require_output_clear(_CHANNELS)
            runners = tuple(self._runners.values())
            try:
                await self._cancel_runner_watchers_locked()
                await self._quiesce_runners(runners, reason="finish", clear=True)
                self._require_estop_inactive()
            except BaseException:
                if not self._live_clear_required:
                    self._set_status(SessionStatus.PAUSED)
                raise

            session_id = self._session_id
            if session_id is None:
                raise RuntimeError("live session has no ID")
            replay_id = self._require_id(self._replay_id_factory(), "replay ID")
            timeline = Timeline(
                schema_version=SCHEMA_VERSION,
                session_id=session_id,
                seed=self.seed,
                plot_events=tuple(self._plot_events),
                cycles=self._completed_cycle_records(),
            )
            manifest = self._completed_manifest(
                replay_id=replay_id,
                session_id=session_id,
            )
            try:
                self.store.save(manifest, timeline)
            except Exception:
                self._set_status(SessionStatus.PAUSED)
                raise

            summary = ReplaySummary.from_manifest(
                manifest, cycle_count=len(timeline.cycles)
            )
            self._reset_idle()
            return summary

    async def on_disconnect(self) -> SessionState:
        async with self._lock:
            if self._status is SessionStatus.PAUSED:
                if self._mode == "replay" and self._player is not None:
                    self._set_status(SessionStatus.FINISHING)
                    await self._player.pause()
                    self._set_status(SessionStatus.PAUSED)
                elif self._mode == "autopilot" and (
                    self._live_clear_required
                    or not self._output_clear_is_confirmed()
                ):
                    await self._pause_live_locked("disconnect_retry")
                return self.to_state()
            if self._status is SessionStatus.FINISHING:
                if self._mode == "replay" and self._player is not None:
                    await self._player.pause()
                    self._set_status(SessionStatus.PAUSED)
                elif self._mode == "autopilot" and self._live_clear_required:
                    await self._pause_live_locked("disconnect_retry")
                return self.to_state()
            if self._status is SessionStatus.REPLAYING:
                if self._player is not None:
                    self._set_status(SessionStatus.FINISHING)
                    await self._player.pause()
                    self._set_status(SessionStatus.PAUSED)
                return self.to_state()
            if self._status is not SessionStatus.RUNNING or self._mode != "autopilot":
                return self.to_state()
            await self._pause_live_locked("disconnect")
            return self.to_state()

    async def start_replay(
        self, replay_id: str, *, cursor: int = 0
    ) -> SessionState:
        async with self._lock:
            if self._status is not SessionStatus.IDLE:
                raise RuntimeError("a timeline session is already active")
            self._require_estop_inactive()
            bundle = self.store.load(replay_id)
            player = self._player_factory(
                executor=self.game_loop,
                clock=self._clock,
                sleeper=self._sleeper,
            )
            player.load(bundle)
            player.validate_cursor(cursor)
            self._set_player_output_generations(player)
            self._player = player
            self._mode = "replay"
            self._set_status(SessionStatus.REPLAYING)
            self._session_id = bundle.manifest.session_id
            self._replay_id = bundle.manifest.replay_id
            self._current_event_id = None
            if cursor:
                await player.resume(cursor)
            else:
                await player.start()
            self._start_player_watcher_locked(player)
            return self.to_state()

    async def stop(self) -> SessionState:
        """Abnormally stop active work without creating replay history."""
        async with self._lock:
            if self._status is SessionStatus.IDLE:
                return self.to_state()
            if self._mode == "replay":
                self._require_output_clear(_CHANNELS)
                if self._player_watcher is not None:
                    self._player_watcher.cancel()
                    if self._player_watcher is not asyncio.current_task():
                        await asyncio.gather(
                            self._player_watcher, return_exceptions=True
                        )
                if self._player is not None:
                    self._set_status(SessionStatus.FINISHING)
                    await self._player.stop()
                self._reset_idle()
                return self.to_state()

            if self._status is SessionStatus.RUNNING:
                await self._pause_live_locked("stop")
            elif self._status in (
                SessionStatus.PAUSED,
                SessionStatus.FINISHING,
            ) and self._live_clear_required:
                await self._pause_live_locked("stop")
            await self._cancel_runner_watchers_locked()
            await asyncio.gather(
                *(
                    runner.stop(clear=False, reason="stop")
                    for runner in self._runners.values()
                ),
                return_exceptions=True,
            )
            self._reset_idle()
            return self.to_state()

    def to_state(self) -> SessionState:
        cursor = (
            self._player.cursor
            if self._mode == "replay" and self._player is not None
            else self._event_cursor
        )
        adjusted = (
            self._player.adjusted
            if self._mode == "replay" and self._player is not None
            else False
        )
        return SessionState(
            status=self._status,
            mode=self._mode,
            session_id=self._session_id,
            replay_id=self._replay_id,
            cursor=cursor,
            current_event_id=self._current_event_id,
            adjusted=adjusted,
            channels=self.channel_states(),
        )

    def channel_states(self) -> dict[str, ChannelPlaybackState]:
        """Expose only operator-safe live/replay scheduling state."""
        if self._mode == "replay" and self._player is not None:
            return {
                channel: ChannelPlaybackState.from_dict(value)
                for channel, value in self._player.channel_states().items()
            }
        if self._mode != "autopilot" or self._status is SessionStatus.IDLE:
            return {channel: ChannelPlaybackState() for channel in _CHANNELS}

        channels: dict[str, ChannelPlaybackState] = {}
        for channel in _CHANNELS:
            runner = self._runners.get(channel)
            runner_state = runner.state() if runner is not None else None
            if self._status is SessionStatus.PAUSED:
                channels[channel] = ChannelPlaybackState(
                    phase="paused",
                    cycle_index=(runner_state.cycle_index if runner_state else 0),
                )
                continue
            if runner_state is None:
                channels[channel] = ChannelPlaybackState()
                continue
            phase = runner_state.phase.value
            directive = runner_state.directive
            active = phase in ("cycle", "gap") and directive is not None
            channels[channel] = ChannelPlaybackState(
                phase=phase,
                pattern=directive.pattern if active else None,
                strength=(
                    int(self.game_loop.safety.current.get(channel, 0))
                    if active and hasattr(self.game_loop, "safety")
                    else 0
                ),
                cycle_index=runner_state.cycle_index,
                next_cycle_start_ms=runner_state.next_cycle_start_ms,
            )
        return channels

    async def _pause_live_locked(self, reason: str) -> None:
        if self._pause_started_at is None:
            self._pause_started_at = self._clock()
        self._live_clear_required = True
        self._set_status(SessionStatus.FINISHING)
        self._require_output_clear(_CHANNELS)
        await self._cancel_runner_watchers_locked()
        try:
            await self._quiesce_runners(
                tuple(self._runners.values()), reason=reason, clear=True
            )
        except BaseException:
            if not self._live_clear_required:
                self._set_status(SessionStatus.PAUSED)
            raise
        self._set_status(SessionStatus.PAUSED)

    async def _resume_live_locked(self) -> None:
        if self._live_clear_required:
            raise RuntimeError("live output clear is still pending")
        if self._pause_started_at is None:
            raise RuntimeError("paused live session has no pause timestamp")
        paused_ms = max(
            0, int(round((self._clock() - self._pause_started_at) * 1000))
        )
        self._paused_total_ms += paused_ms
        self._pause_started_at = None
        self._set_status(SessionStatus.RUNNING)
        self._runners = {}
        for channel in _CHANNELS:
            runner = self._install_runner(channel)
            directive = self._retained.get(channel)
            if directive is not None:
                await runner.submit(directive)

    def _install_runner(self, channel: str) -> ChannelCycleRunner:
        previous = self._runner_watchers.get(channel)
        if previous is not None and not previous.done():
            raise RuntimeError("runner cleanup must finish before replacement")
        self._runner_watchers.pop(channel, None)
        self._claim_output_generations((channel,))
        base_index = max(
            (
                record.cycle_index
                for record in self._cycle_records.values()
                if record.channel == channel
            ),
            default=0,
        )
        session_id = self._session_id

        def record_cycle(record: CycleRecord) -> None:
            if self._session_id != session_id:
                return
            global_record = replace(
                record, cycle_index=base_index + record.cycle_index
            )
            key = (global_record.channel, global_record.cycle_index)
            self._cycle_records[key] = global_record

        runner = ChannelCycleRunner(
            channel=channel,
            policy=self.policy,
            rng=self._cycle_rngs[channel],
            frames=self._frames,
            executor=self._runner_executor,
            clock=lambda: self._active_offset_ms() / 1000,
            sleeper=self._sleeper,
            on_cycle=record_cycle,
        )
        self._runners[channel] = runner
        watcher = asyncio.create_task(
            self._watch_runner(channel, runner),
            name=f"timeline-session-watch-{channel}",
        )
        self._runner_watchers[channel] = watcher
        return runner

    async def _retire_runner_locked(self, channel: str, reason: str) -> None:
        watcher = self._runner_watchers.pop(channel, None)
        if watcher is not None and watcher is not asyncio.current_task():
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        runner = self._runners.get(channel)
        if runner is None:
            return
        await self._quiesce_runners((runner,), reason=reason, clear=False)
        if self._runners.get(channel) is runner:
            self._runners.pop(channel, None)

    async def _quiesce_runners(
        self,
        runners: Sequence[ChannelCycleRunner],
        *,
        reason: str,
        clear: bool,
    ) -> None:
        cleanup = asyncio.create_task(
            self._terminalize_then_clear_runners(
                runners, reason=reason, clear=clear
            ),
            name="timeline-session-runner-cleanup",
        )
        cancellation: asyncio.CancelledError | None = None
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as exc:
            cancellation = exc
            await cleanup
        if cancellation is not None:
            raise cancellation

    async def _terminalize_then_clear_runners(
        self,
        runners: Sequence[ChannelCycleRunner],
        *,
        reason: str,
        clear: bool,
    ) -> None:
        pause_results = await asyncio.gather(
            *(runner.pause(reason=reason) for runner in runners),
            return_exceptions=True,
        )
        stop_results = await asyncio.gather(
            *(runner.stop(clear=False, reason=reason) for runner in runners),
            return_exceptions=True,
        )
        stop_error = next(
            (result for result in stop_results if isinstance(result, BaseException)),
            None,
        )
        if stop_error is not None:
            raise stop_error
        if clear:
            await self._clear_live_output_locked()
        pause_error = next(
            (result for result in pause_results if isinstance(result, BaseException)),
            None,
        )
        if pause_error is not None:
            raise pause_error

    async def _watch_runner(
        self, channel: str, runner: ChannelCycleRunner
    ) -> None:
        try:
            state = await runner.wait_stopped()
            async with self._lock:
                if (
                    self._status is SessionStatus.RUNNING
                    and self._mode == "autopilot"
                    and self._runners.get(channel) is runner
                ):
                    await self._settle_stopped_runner_locked(channel, runner, state)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            if self._runner_watchers.get(channel) is asyncio.current_task():
                self._runner_watchers.pop(channel, None)

    async def _watch_player(self, player: RecordedCyclePlayer) -> None:
        try:
            await player.wait()
        except asyncio.CancelledError:
            return
        except Exception:
            async with self._lock:
                if self._player is player and self._status is SessionStatus.REPLAYING:
                    self._set_status(
                        SessionStatus.PAUSED
                        if player.cleared
                        else SessionStatus.FINISHING
                    )
            return
        async with self._lock:
            if self._player is player and self._status is SessionStatus.REPLAYING:
                self._reset_idle()

    def _start_player_watcher_locked(self, player: RecordedCyclePlayer) -> None:
        previous = self._player_watcher
        if previous is not None and not previous.done():
            previous.cancel()
        self._player_watcher = asyncio.create_task(
            self._watch_player(player), name="timeline-replay-watch"
        )

    async def _cancel_runner_watchers_locked(self) -> None:
        current = asyncio.current_task()
        tasks = [
            watcher
            for watcher in self._runner_watchers.values()
            if watcher is not current and not watcher.done()
        ]
        for watcher in tasks:
            watcher.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._runner_watchers = {
            channel: watcher
            for channel, watcher in self._runner_watchers.items()
            if watcher is current and not watcher.done()
        }

    async def _settle_stopped_runner_locked(
        self,
        channel: str,
        runner: ChannelCycleRunner,
        state: Any | None = None,
    ) -> None:
        if self._runners.get(channel) is not runner:
            return
        state = runner.state() if state is None else state
        if state.disconnected:
            await self._pause_live_locked("disconnect")
            return
        if state.failure is not None:
            try:
                result = await self.game_loop.clear_output(channel)
                self._require_clear_result(result, channel)
            except Exception:
                await self._pause_live_locked("runner_clear_failure")
                return
        await self._discard_runner_locked(channel, runner)

    async def _discard_runner_locked(
        self, channel: str, runner: ChannelCycleRunner
    ) -> None:
        if self._runners.get(channel) is runner:
            self._runners.pop(channel, None)
        watcher = self._runner_watchers.pop(channel, None)
        if watcher is None or watcher is asyncio.current_task():
            return
        if not watcher.done():
            watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)

    async def _clear_live_output_locked(self) -> None:
        self._live_clear_required = True
        self._require_output_clear(_CHANNELS)
        result = await self.game_loop.clear_output()
        self._require_clear_result(result)
        self._live_clear_required = False

    def _claim_output_generations(
        self, channels: Sequence[str]
    ) -> dict[str, int]:
        begin = getattr(self.game_loop, "begin_timeline_output", None)
        if not callable(begin):
            return {}
        generations = begin(tuple(channels))
        if not isinstance(generations, Mapping):
            raise TypeError("timeline output owner must be a mapping")
        claimed = {
            str(channel): int(generation)
            for channel, generation in generations.items()
        }
        expected = {str(channel) for channel in channels}
        if set(claimed) != expected:
            raise ValueError(
                "timeline output owner must provide every requested channel"
            )
        self._output_generations.update(claimed)
        self._runner_executor.update_owner_generations(claimed)
        return claimed

    def _set_player_output_generations(
        self, player: RecordedCyclePlayer
    ) -> None:
        generations = self._claim_output_generations(_CHANNELS)
        setter = getattr(player, "set_output_generations", None)
        if callable(setter) and generations:
            setter(generations)

    def _require_output_clear(self, channels: Sequence[str]) -> None:
        require_clear = getattr(self.game_loop, "require_output_clear", None)
        if callable(require_clear):
            require_clear(tuple(channels))

    def _output_clear_is_confirmed(self) -> bool:
        is_confirmed = getattr(self.game_loop, "output_clear_is_confirmed", None)
        if not callable(is_confirmed):
            return True
        try:
            return bool(is_confirmed(_CHANNELS))
        except Exception:
            return False

    @staticmethod
    def _require_clear_result(result: object, channel: str | None = None) -> None:
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or not isinstance(result[0], Sequence)
            or isinstance(result[0], (str, bytes))
            or not isinstance(result[1], Sequence)
            or isinstance(result[1], (str, bytes))
            or result[1]
        ):
            raise RuntimeError("output clear was not confirmed")
        expected = (
            {"op": "stop"}
            if channel is None
            else {"op": "clear", "channel": channel}
        )
        if not any(
            isinstance(item, Mapping)
            and isinstance(item.get("action"), Mapping)
            and dict(item["action"]) == expected
            for item in result[0]
        ):
            raise RuntimeError("output clear was not confirmed")

    def _completed_cycle_records(self) -> tuple[CycleRecord, ...]:
        records = [record for record in self._cycle_records.values() if record.completed]
        return tuple(
            record
            for _, record in sorted(
                enumerate(records),
                key=lambda item: (item[1].active_start_offset_ms, item[0]),
            )
        )

    def _completed_manifest(
        self, *, replay_id: str, session_id: str
    ) -> ReplayManifest:
        allowed_metadata = {
            key: value
            for key, value in self._manifest_metadata.items()
            if key
            in {
                "app_commit",
                "model",
                "dlc_role",
                "dlc_profile",
                "dlc_version",
            }
        }
        return ReplayManifest(
            schema_version=SCHEMA_VERSION,
            replay_id=replay_id,
            session_id=session_id,
            seed=self.seed,
            status=SessionStatus.COMPLETED,
            mode="autopilot",
            random_profile={
                "strength_jitter": self.strength_jitter,
                "waveform_policy": self.waveform_policy,
                "cycle_gap": self.policy.to_dict(),
            },
            safety_caps=dict(self._safety_caps),
            created_at=self._created_at,
            completed_at=self._timestamp_factory(),
            adjusted=False,
            **allowed_metadata,
        )

    def _new_cycle_rngs(self) -> dict[str, Any]:
        if self._provided_cycle_rngs is not None:
            return dict(self._provided_cycle_rngs)
        return {
            channel: random.Random(
                derive_stream_seed(self.seed, f"cycle:{channel}")
            )
            for channel in _CHANNELS
        }

    def _active_offset_ms(self) -> int:
        if self._active_started_at is None:
            return 0
        end = self._pause_started_at
        if end is None:
            end = self._clock()
        elapsed_ms = int(round((end - self._active_started_at) * 1000))
        return max(0, elapsed_ms - self._paused_total_ms)

    def _submission_is_current(
        self,
        channel: str,
        runner: ChannelCycleRunner,
        directive: CycleDirective,
    ) -> bool:
        return (
            self._status is SessionStatus.RUNNING
            and self._mode == "autopilot"
            and self._runners.get(channel) is runner
            and self._retained.get(channel) == directive
        )

    def _require_estop_inactive(self) -> None:
        if bool(
            getattr(getattr(self.game_loop, "safety", None), "estop_active", False)
        ):
            raise RuntimeError(
                "emergency stop is active; resume it through GameLoop first"
            )

    def _current_strengths(self) -> dict[str, int]:
        current = getattr(getattr(self.game_loop, "safety", None), "current", {})
        return {channel: int(current.get(channel, 0)) for channel in _CHANNELS}

    def _current_caps(self) -> dict[str, int]:
        safety = getattr(self.game_loop, "safety", None)
        if safety is None or not hasattr(safety, "cap_for"):
            raise TypeError("game_loop.safety must provide cap_for(channel)")
        return {channel: int(safety.cap_for(channel)) for channel in _CHANNELS}

    def _enabled_channels(self) -> dict[str, bool]:
        enabled = getattr(getattr(self.game_loop, "safety", None), "enabled", {})
        return {channel: bool(enabled.get(channel, False)) for channel in _CHANNELS}

    def _normalize_frames(
        self, frames: Mapping[str, Sequence[str]] | None
    ) -> dict[str, tuple[str, ...]]:
        if frames is None:
            presets = getattr(getattr(self.game_loop, "safety", None), "presets", {})
            frames = {
                pattern: metadata.get("frames", ())
                for pattern, metadata in presets.items()
                if isinstance(metadata, Mapping)
            }
        normalized = {
            pattern: tuple(values)
            for pattern, values in frames.items()
            if isinstance(pattern, str) and pattern.strip()
        }
        if not normalized:
            raise ValueError("at least one waveform is required")
        return normalized

    def _reset_idle(self) -> None:
        self._set_status(SessionStatus.IDLE)
        self._mode = None
        self._session_id = None
        self._replay_id = None
        self._resolver = None
        self._cycle_rngs = {}
        self._runners = {}
        self._runner_watchers = {}
        self._player = None
        self._player_watcher = None
        self._plot_events = []
        self._cycle_records = {}
        self._retained = {}
        self._event_cursor = 0
        self._current_event_id = None
        self._created_at = ""
        self._active_started_at = None
        self._pause_started_at = None
        self._paused_total_ms = 0
        self._safety_caps = {}
        self._live_clear_required = False
        self._output_generations = {}
        self._runner_executor.clear_owner_generations()

    def _set_status(self, status: SessionStatus) -> None:
        if self._status is not status:
            self._routing_generation += 1
        self._status = status

    @staticmethod
    def _require_id(value: object, name: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty string")
        return value
