"""Versioned, serializable domain records for timeline sessions and replays."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import random
from typing import Any, Mapping


SCHEMA_VERSION = 1
_CHANNELS = frozenset(("A", "B"))


def _require_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _require_schema(data: Mapping[str, Any]) -> None:
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported timeline schema version: {data.get('schema_version')!r}")


def _require_channel(channel: object) -> str:
    if not isinstance(channel, str) or channel not in _CHANNELS:
        raise ValueError("channel must be A or B")
    return str(channel)


def _require_non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _optional_strength(value: object, name: str) -> int | None:
    if value is None:
        return None
    strength = _require_non_negative_int(value, name)
    if strength > 200:
        raise ValueError(f"{name} must be in 0..200")
    return strength


def _required_strength(value: object, name: str) -> int:
    strength = _optional_strength(value, name)
    if strength is None:
        raise ValueError(f"{name} must be in 0..200")
    return strength


@dataclass(frozen=True)
class CycleGapPolicy:
    """The one project-wide, integer-tenths cycle-gap distribution."""

    zero_weight: int = 40
    short_weight: int = 30
    long_weight: int = 30
    frame_ms: int = 100

    def __post_init__(self) -> None:
        weights = (self.zero_weight, self.short_weight, self.long_weight)
        if (
            any(isinstance(weight, bool) or not isinstance(weight, int) or weight < 0 for weight in weights)
            or sum(weights) != 100
        ):
            raise ValueError("cycle-gap weights must be non-negative and total 100")
        if isinstance(self.frame_ms, bool) or not isinstance(self.frame_ms, int) or self.frame_ms != 100:
            raise ValueError("DG-LAB waveform frames must remain 100 ms")

    def sample_tenths(self, rng: random.Random) -> int:
        roll = rng.randrange(100)
        if roll < self.zero_weight:
            return 0
        if roll < self.zero_weight + self.short_weight:
            return rng.randint(1, 10)
        return rng.randint(11, 20)

    def cycle_ms(self, frame_count: int) -> int:
        if isinstance(frame_count, bool) or not isinstance(frame_count, int) or frame_count <= 0:
            raise ValueError("frame_count must be positive")
        return frame_count * self.frame_ms

    def gap_ms(self, frame_count: int, tenths: int) -> int:
        if isinstance(tenths, bool) or not isinstance(tenths, int) or not 0 <= tenths <= 20:
            raise ValueError("gap tenths must be in 0..20")
        return self.cycle_ms(frame_count) * tenths // 10

    def to_dict(self) -> dict[str, int]:
        return {
            "zero_weight": self.zero_weight,
            "short_weight": self.short_weight,
            "long_weight": self.long_weight,
            "frame_ms": self.frame_ms,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CycleGapPolicy:
        data = _require_mapping(data, "cycle_gap")
        return cls(
            zero_weight=data.get("zero_weight", 40),
            short_weight=data.get("short_weight", 30),
            long_weight=data.get("long_weight", 30),
            frame_ms=data.get("frame_ms", 100),
        )


class DirectiveMode(str, Enum):
    KEEP = "keep"
    SET = "set"
    STOP = "stop"


@dataclass(frozen=True)
class ChannelDirective:
    channel: str
    mode: DirectiveMode
    pattern: str | None = None
    base_strength: int | None = None
    resolved_strength: int | None = None

    def __post_init__(self) -> None:
        _require_channel(self.channel)
        if not isinstance(self.mode, DirectiveMode):
            raise ValueError("mode must be a DirectiveMode")
        if self.pattern is not None and (not isinstance(self.pattern, str) or not self.pattern.strip()):
            raise ValueError("pattern must be a non-empty string when provided")
        _optional_strength(self.base_strength, "base_strength")
        _optional_strength(self.resolved_strength, "resolved_strength")
        if self.mode is DirectiveMode.SET:
            if self.pattern is None or self.base_strength is None or self.resolved_strength is None:
                raise ValueError("set directives require pattern, base_strength, and resolved_strength")
        elif any(value is not None for value in (self.pattern, self.base_strength, self.resolved_strength)):
            raise ValueError("keep and stop directives cannot include resolved waveform data")

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "mode": self.mode.value,
            "pattern": self.pattern,
            "base_strength": self.base_strength,
            "resolved_strength": self.resolved_strength,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ChannelDirective:
        data = _require_mapping(data, "channel directive")
        try:
            mode = DirectiveMode(data["mode"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("channel directive has an invalid mode") from exc
        return cls(
            channel=data.get("channel"),
            mode=mode,
            pattern=data.get("pattern"),
            base_strength=data.get("base_strength"),
            resolved_strength=data.get("resolved_strength"),
        )


@dataclass(frozen=True)
class PlotEvent:
    event_id: str
    scene_id: str
    offset_ms: int
    channels: Mapping[str, ChannelDirective]

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id:
            raise ValueError("event_id must be a non-empty string")
        if not isinstance(self.scene_id, str) or not self.scene_id:
            raise ValueError("scene_id must be a non-empty string")
        _require_non_negative_int(self.offset_ms, "offset_ms")
        channels = _require_mapping(self.channels, "channels")
        if not channels:
            raise ValueError("channels must not be empty")
        for channel, directive in channels.items():
            _require_channel(channel)
            if not isinstance(directive, ChannelDirective) or directive.channel != channel:
                raise ValueError("each channel needs its matching ChannelDirective")

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "scene_id": self.scene_id,
            "offset_ms": self.offset_ms,
            "channels": {channel: directive.to_dict() for channel, directive in self.channels.items()},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PlotEvent:
        data = _require_mapping(data, "plot event")
        channels = _require_mapping(data.get("channels"), "plot event channels")
        return cls(
            event_id=data.get("event_id"),
            scene_id=data.get("scene_id"),
            offset_ms=data.get("offset_ms"),
            channels={channel: ChannelDirective.from_dict(value) for channel, value in channels.items()},
        )


@dataclass(frozen=True)
class CycleRecord:
    channel: str
    cycle_index: int
    plot_event_id: str
    pattern: str
    waveform_hash: str
    requested_strength: int
    effective_strength: int
    active_start_offset_ms: int
    raw_duration_ms: int
    gap_tenths: int
    planned_gap_ms: int
    actual_gap_ms: int
    completed: bool
    interruption_reason: str | None

    def __post_init__(self) -> None:
        _require_channel(self.channel)
        if isinstance(self.cycle_index, bool) or not isinstance(self.cycle_index, int) or self.cycle_index <= 0:
            raise ValueError("cycle_index must be positive")
        for value, name in ((self.plot_event_id, "plot_event_id"), (self.pattern, "pattern"), (self.waveform_hash, "waveform_hash")):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        _required_strength(self.requested_strength, "requested_strength")
        _required_strength(self.effective_strength, "effective_strength")
        _require_non_negative_int(self.active_start_offset_ms, "active_start_offset_ms")
        if isinstance(self.raw_duration_ms, bool) or not isinstance(self.raw_duration_ms, int) or self.raw_duration_ms <= 0:
            raise ValueError("raw_duration_ms must be positive")
        if isinstance(self.gap_tenths, bool) or not isinstance(self.gap_tenths, int) or not 0 <= self.gap_tenths <= 20:
            raise ValueError("gap_tenths must be in 0..20")
        _require_non_negative_int(self.planned_gap_ms, "planned_gap_ms")
        _require_non_negative_int(self.actual_gap_ms, "actual_gap_ms")
        if not isinstance(self.completed, bool):
            raise ValueError("completed must be a boolean")
        if self.interruption_reason is not None and (not isinstance(self.interruption_reason, str) or not self.interruption_reason):
            raise ValueError("interruption_reason must be a non-empty string when provided")

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "cycle_index": self.cycle_index,
            "plot_event_id": self.plot_event_id,
            "pattern": self.pattern,
            "waveform_hash": self.waveform_hash,
            "requested_strength": self.requested_strength,
            "effective_strength": self.effective_strength,
            "active_start_offset_ms": self.active_start_offset_ms,
            "raw_duration_ms": self.raw_duration_ms,
            "gap_tenths": self.gap_tenths,
            "planned_gap_ms": self.planned_gap_ms,
            "actual_gap_ms": self.actual_gap_ms,
            "completed": self.completed,
            "interruption_reason": self.interruption_reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CycleRecord:
        data = _require_mapping(data, "cycle record")
        try:
            return cls(**{field: data[field] for field in cls.__dataclass_fields__})
        except KeyError as exc:
            raise ValueError(f"cycle record is missing {exc.args[0]}") from exc


@dataclass(frozen=True)
class Timeline:
    schema_version: int
    session_id: str
    seed: int
    plot_events: tuple[PlotEvent, ...]
    cycles: tuple[CycleRecord, ...]

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported timeline schema version: {self.schema_version!r}")
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("session_id must be a non-empty string")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be an integer")
        if not isinstance(self.plot_events, tuple) or not all(isinstance(event, PlotEvent) for event in self.plot_events):
            raise ValueError("plot_events must be a tuple of PlotEvent values")
        if not isinstance(self.cycles, tuple) or not all(isinstance(cycle, CycleRecord) for cycle in self.cycles):
            raise ValueError("cycles must be a tuple of CycleRecord values")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "seed": self.seed,
            "plot_events": [event.to_dict() for event in self.plot_events],
            "cycles": [cycle.to_dict() for cycle in self.cycles],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Timeline:
        data = _require_mapping(data, "timeline")
        _require_schema(data)
        events = data.get("plot_events")
        cycles = data.get("cycles")
        if not isinstance(events, list) or not isinstance(cycles, list):
            raise ValueError("timeline plot_events and cycles must be arrays")
        return cls(
            schema_version=data["schema_version"],
            session_id=data.get("session_id"),
            seed=data.get("seed"),
            plot_events=tuple(PlotEvent.from_dict(event) for event in events),
            cycles=tuple(CycleRecord.from_dict(cycle) for cycle in cycles),
        )


class SessionStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHING = "finishing"
    COMPLETED = "completed"
    REPLAYING = "replaying"


@dataclass(frozen=True)
class ReplayManifest:
    schema_version: int
    replay_id: str
    session_id: str
    seed: int
    status: SessionStatus
    mode: str
    app_commit: str = ""
    model: str = ""
    dlc_role: str = ""
    dlc_profile: str = ""
    dlc_version: str = ""
    random_profile: Mapping[str, Any] | None = None
    safety_caps: Mapping[str, int] | None = None
    created_at: str = ""
    completed_at: str | None = None
    source_hash: str | None = None
    checksums: Mapping[str, str] | None = None
    adjusted: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported replay schema version: {self.schema_version!r}")
        for value, name in ((self.replay_id, "replay_id"), (self.session_id, "session_id"), (self.mode, "mode")):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be an integer")
        if not isinstance(self.status, SessionStatus):
            raise ValueError("status must be a SessionStatus")
        for value, name in (
            (self.app_commit, "app_commit"),
            (self.model, "model"),
            (self.dlc_role, "dlc_role"),
            (self.dlc_profile, "dlc_profile"),
            (self.dlc_version, "dlc_version"),
            (self.created_at, "created_at"),
        ):
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
        for value, name in ((self.completed_at, "completed_at"), (self.source_hash, "source_hash")):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be a string when provided")
        for value, name in (
            (self.random_profile, "random_profile"),
            (self.safety_caps, "safety_caps"),
            (self.checksums, "checksums"),
        ):
            if value is not None:
                mapping = _require_mapping(value, name)
                if not all(isinstance(key, str) for key in mapping):
                    raise ValueError(f"{name} keys must be strings")
        if self.safety_caps is not None and any(
            isinstance(cap, bool) or not isinstance(cap, int) or not 0 <= cap <= 200
            for cap in self.safety_caps.values()
        ):
            raise ValueError("safety_caps values must be strengths in 0..200")
        if self.checksums is not None and any(
            not isinstance(checksum, str) for checksum in self.checksums.values()
        ):
            raise ValueError("checksums values must be strings")
        if not isinstance(self.adjusted, bool):
            raise ValueError("adjusted must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "replay_id": self.replay_id,
            "session_id": self.session_id,
            "seed": self.seed,
            "status": self.status.value,
            "mode": self.mode,
            "app_commit": self.app_commit,
            "model": self.model,
            "dlc_role": self.dlc_role,
            "dlc_profile": self.dlc_profile,
            "dlc_version": self.dlc_version,
            "random_profile": dict(self.random_profile or {}),
            "safety_caps": dict(self.safety_caps or {}),
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "source_hash": self.source_hash,
            "checksums": dict(self.checksums or {}),
            "adjusted": self.adjusted,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReplayManifest:
        data = _require_mapping(data, "replay manifest")
        _require_schema(data)
        try:
            status = SessionStatus(data["status"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("replay manifest has an invalid status") from exc
        values = {
            "schema_version": data["schema_version"],
            "replay_id": data.get("replay_id"),
            "session_id": data.get("session_id"),
            "seed": data.get("seed"),
            "status": status,
            "mode": data.get("mode"),
        }
        for field in (
            "app_commit", "model", "dlc_role", "dlc_profile", "dlc_version",
            "random_profile", "safety_caps", "created_at", "completed_at",
            "source_hash", "checksums", "adjusted",
        ):
            if field in data:
                values[field] = data[field]
        return cls(**values)


@dataclass(frozen=True)
class ChannelPlaybackState:
    """Public, redacted progress for one live or replay channel."""

    phase: str = "idle"
    pattern: str | None = None
    strength: int = 0
    cycle_index: int = 0
    next_cycle_start_ms: int | None = None

    def __post_init__(self) -> None:
        if self.phase not in ("idle", "cycle", "gap", "paused", "stopped"):
            raise ValueError("channel phase is invalid")
        if self.pattern is not None and (
            not isinstance(self.pattern, str) or not self.pattern.strip()
        ):
            raise ValueError("channel pattern must be a non-empty string or None")
        _required_strength(self.strength, "channel strength")
        _require_non_negative_int(self.cycle_index, "channel cycle_index")
        if self.next_cycle_start_ms is not None:
            _require_non_negative_int(
                self.next_cycle_start_ms, "channel next_cycle_start_ms"
            )
        if self.phase not in ("cycle", "gap") and (
            self.pattern is not None or self.strength != 0
        ):
            raise ValueError("inactive channel state must redact output")

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "pattern": self.pattern,
            "strength": self.strength,
            "cycle_index": self.cycle_index,
            "next_cycle_start_ms": self.next_cycle_start_ms,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ChannelPlaybackState:
        data = _require_mapping(data, "channel playback state")
        return cls(
            phase=data.get("phase", "idle"),
            pattern=data.get("pattern"),
            strength=data.get("strength", 0),
            cycle_index=data.get("cycle_index", 0),
            next_cycle_start_ms=data.get("next_cycle_start_ms"),
        )


def _idle_channel_states() -> dict[str, ChannelPlaybackState]:
    return {channel: ChannelPlaybackState() for channel in _CHANNELS}


@dataclass(frozen=True)
class SessionState:
    status: SessionStatus
    mode: str | None = None
    session_id: str | None = None
    replay_id: str | None = None
    cursor: int = 0
    current_event_id: str | None = None
    adjusted: bool = False
    channels: Mapping[str, ChannelPlaybackState] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, SessionStatus):
            raise ValueError("status must be a SessionStatus")
        if self.mode is not None and self.mode not in ("autopilot", "replay"):
            raise ValueError("mode must be autopilot, replay, or None")
        _require_non_negative_int(self.cursor, "cursor")
        if not isinstance(self.adjusted, bool):
            raise ValueError("adjusted must be a boolean")
        channels = _idle_channel_states() if self.channels is None else self.channels
        if not isinstance(channels, Mapping) or set(channels) != _CHANNELS:
            raise ValueError("channels must provide A and B")
        normalized: dict[str, ChannelPlaybackState] = {}
        for channel in _CHANNELS:
            value = channels[channel]
            if not isinstance(value, ChannelPlaybackState):
                raise ValueError("channels must contain ChannelPlaybackState values")
            normalized[channel] = value
        object.__setattr__(self, "channels", normalized)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "mode": self.mode,
            "session_id": self.session_id,
            "replay_id": self.replay_id,
            "cursor": self.cursor,
            "current_event_id": self.current_event_id,
            "adjusted": self.adjusted,
            "channels": {
                channel: self.channels[channel].to_dict()
                for channel in ("A", "B")
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SessionState:
        data = _require_mapping(data, "session state")
        try:
            status = SessionStatus(data["status"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("session state has an invalid status") from exc
        return cls(
            status=status,
            mode=data.get("mode"),
            session_id=data.get("session_id"),
            replay_id=data.get("replay_id"),
            cursor=data.get("cursor", 0),
            current_event_id=data.get("current_event_id"),
            adjusted=data.get("adjusted", False),
            channels=(
                {
                    channel: ChannelPlaybackState.from_dict(data["channels"][channel])
                    for channel in ("A", "B")
                }
                if "channels" in data
                else None
            ),
        )
