"""Serialize device output and publish only transport-confirmed channel state."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import IntEnum
from types import MappingProxyType
from typing import TypeAlias


_CHANNELS = ("A", "B")
_EMPTY_EFFECTIVE: Mapping[str, object] = MappingProxyType({})
__all__ = [
    "ConfirmedChannelOutput",
    "DeviceOutputCoordinator",
    "OutputIntentKind",
    "PendingSafetyWork",
    "TransportOutcome",
]


class OutputIntentKind(IntEnum):
    MANUAL = 10
    TIMELINE_OR_REPLAY = 20
    SAFETY_REDUCE = 30
    CLEAR_OR_DISABLE = 40
    ESTOP = 50


def _freeze_effective(value: Mapping[str, object]) -> Mapping[str, object]:
    frozen: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("effective state keys must be strings")
        if isinstance(item, Mapping):
            frozen[key] = _freeze_effective(item)
        elif item is None or isinstance(item, (bool, int, float, str)):
            frozen[key] = item
        else:
            raise ValueError("effective state must contain only state values")
    return MappingProxyType(frozen)


@dataclass(frozen=True)
class TransportOutcome:
    """Immutable result of one transport operation."""

    sent: bool
    effective: Mapping[str, object] | None = None
    simulated: bool = False
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.sent, bool):
            raise ValueError("sent must be a boolean")
        if not isinstance(self.simulated, bool):
            raise ValueError("simulated must be a boolean")
        if self.effective is not None:
            if not isinstance(self.effective, Mapping):
                raise ValueError("effective must be a mapping when provided")
            object.__setattr__(self, "effective", _freeze_effective(self.effective))
        if self.error is not None and not isinstance(self.error, str):
            raise ValueError("error must be a string when provided")


@dataclass(frozen=True)
class ConfirmedChannelOutput:
    """Public snapshot of state confirmed by successful device transport."""

    strength: int | None = None
    waveform: str | None = None
    waveform_mode: str | None = None
    enabled: bool = True


@dataclass(frozen=True)
class PendingSafetyWork:
    """Retryable safety state that has not yet been physically confirmed."""

    target_strength: int | None = None
    clear_required: bool = False


@dataclass
class _ChannelSlot:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    confirmed: ConfirmedChannelOutput = field(default_factory=ConfirmedChannelOutput)
    pending: PendingSafetyWork = field(default_factory=PendingSafetyWork)
    generation: int = 0
    minimum_priority: OutputIntentKind = OutputIntentKind.MANUAL
    estop_latched: bool = False


_ChannelOperation: TypeAlias = Callable[
    [ConfirmedChannelOutput], Awaitable[TransportOutcome]
]
_GlobalOperation: TypeAlias = Callable[
    [Mapping[str, ConfirmedChannelOutput]], Awaitable[TransportOutcome]
]


class DeviceOutputCoordinator:
    """Own per-channel transport serialization and confirmed output state."""

    def __init__(self) -> None:
        self._slots = {channel: _ChannelSlot() for channel in _CHANNELS}

    def seed_confirmed(
        self,
        channel: str,
        *,
        strength: int | None = None,
        waveform: str | None = None,
        waveform_mode: str | None = None,
        enabled: bool = True,
    ) -> None:
        slot = self._slot(channel)
        slot.confirmed = ConfirmedChannelOutput(
            strength=_strength(strength),
            waveform=_optional_text(waveform, "waveform"),
            waveform_mode=_optional_text(waveform_mode, "waveform_mode"),
            enabled=_enabled(enabled),
        )

    def generation(self, channel: str) -> int:
        return self._slot(channel).generation

    def is_current(self, channel: str, generation: int) -> bool:
        return self._slot(channel).generation == generation

    def invalidate(
        self, channel: str, minimum_priority: OutputIntentKind
    ) -> int:
        slot = self._slot(channel)
        kind = _kind(minimum_priority)
        slot.generation += 1
        if kind > slot.minimum_priority:
            slot.minimum_priority = kind
        if kind is OutputIntentKind.ESTOP:
            slot.estop_latched = True
        return slot.generation

    def require_clear(self, channel: str) -> None:
        slot = self._slot(channel)
        slot.pending = replace(slot.pending, clear_required=True)
        self.invalidate(channel, OutputIntentKind.CLEAR_OR_DISABLE)

    def mark_reduction(self, channel: str, target: int) -> None:
        slot = self._slot(channel)
        slot.pending = replace(slot.pending, target_strength=_strength(target))
        self.invalidate(channel, OutputIntentKind.SAFETY_REDUCE)

    def confirmed(self, channel: str) -> ConfirmedChannelOutput:
        return self._slot(channel).confirmed

    def pending(self, channel: str) -> PendingSafetyWork:
        return self._slot(channel).pending

    async def run(
        self,
        channel: str,
        kind: OutputIntentKind,
        operation: _ChannelOperation,
    ) -> TransportOutcome:
        slot = self._slot(channel)
        intent = _kind(kind)
        self._prepare_safety_intent(channel, intent)
        started_generation = slot.generation
        task = asyncio.create_task(
            self._run_channel_locked(slot, started_generation, intent, operation)
        )
        return await _await_cleanup(task)

    async def run_global(
        self,
        kind: OutputIntentKind,
        operation: _GlobalOperation,
    ) -> TransportOutcome:
        """Run one two-channel transport while acquiring A before B."""

        intent = _kind(kind)
        for channel in _CHANNELS:
            self._prepare_safety_intent(channel, intent)
        generations = {
            channel: self._slots[channel].generation for channel in _CHANNELS
        }
        task = asyncio.create_task(
            self._run_global_locked(generations, intent, operation)
        )
        return await _await_cleanup(task)

    def _prepare_safety_intent(
        self, channel: str, kind: OutputIntentKind
    ) -> None:
        if kind < OutputIntentKind.SAFETY_REDUCE:
            return
        slot = self._slot(channel)
        if kind >= OutputIntentKind.CLEAR_OR_DISABLE:
            slot.pending = replace(slot.pending, clear_required=True)
        self.invalidate(channel, kind)

    async def _run_channel_locked(
        self,
        slot: _ChannelSlot,
        started_generation: int,
        kind: OutputIntentKind,
        operation: _ChannelOperation,
    ) -> TransportOutcome:
        async with slot.lock:
            rejection = self._rejection(slot, started_generation, kind)
            if rejection is not None:
                return rejection
            outcome = await self._execute(operation, slot.confirmed)
            return self._commit(slot, kind, outcome)

    async def _run_global_locked(
        self,
        generations: Mapping[str, int],
        kind: OutputIntentKind,
        operation: _GlobalOperation,
    ) -> TransportOutcome:
        slot_a = self._slots["A"]
        slot_b = self._slots["B"]
        async with slot_a.lock:
            async with slot_b.lock:
                for channel in _CHANNELS:
                    rejection = self._rejection(
                        self._slots[channel], generations[channel], kind
                    )
                    if rejection is not None:
                        return rejection
                snapshots = MappingProxyType(
                    {channel: self._slots[channel].confirmed for channel in _CHANNELS}
                )
                outcome = await self._execute(operation, snapshots)
                if not outcome.sent:
                    return _failure(outcome.error, simulated=outcome.simulated)
                effective = outcome.effective or _EMPTY_EFFECTIVE
                for channel in _CHANNELS:
                    channel_effective = effective.get(channel)
                    if isinstance(channel_effective, Mapping):
                        self._commit_sent(
                            self._slots[channel], kind, channel_effective
                        )
                return outcome

    @staticmethod
    async def _execute(operation, snapshot) -> TransportOutcome:
        try:
            outcome = await operation(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _failure(str(exc) or type(exc).__name__)
        if not isinstance(outcome, TransportOutcome):
            return _failure("transport operation returned an invalid outcome")
        return outcome

    def _commit(
        self,
        slot: _ChannelSlot,
        kind: OutputIntentKind,
        outcome: TransportOutcome,
    ) -> TransportOutcome:
        if not outcome.sent:
            if (
                kind is OutputIntentKind.SAFETY_REDUCE
                and slot.pending.target_strength is None
                and outcome.effective is not None
            ):
                target = outcome.effective.get("strength")
                if isinstance(target, int) and not isinstance(target, bool):
                    slot.pending = replace(
                        slot.pending, target_strength=_strength(target)
                    )
                    self._raise_priority(slot, OutputIntentKind.SAFETY_REDUCE)
            return _failure(outcome.error, simulated=outcome.simulated)
        self._commit_sent(slot, kind, outcome.effective or _EMPTY_EFFECTIVE)
        return outcome

    def _commit_sent(
        self,
        slot: _ChannelSlot,
        kind: OutputIntentKind,
        effective: Mapping[str, object],
    ) -> None:
        changes: dict[str, object] = {}
        if "strength" in effective:
            changes["strength"] = _strength(effective["strength"])
        if "waveform" in effective:
            changes["waveform"] = _optional_text(
                effective["waveform"], "waveform"
            )
        if "waveform_mode" in effective:
            changes["waveform_mode"] = _optional_text(
                effective["waveform_mode"], "waveform_mode"
            )
        if "enabled" in effective:
            changes["enabled"] = _enabled(effective["enabled"])
        if changes:
            slot.confirmed = replace(slot.confirmed, **changes)

        pending = slot.pending
        if kind >= OutputIntentKind.CLEAR_OR_DISABLE:
            pending = replace(pending, clear_required=False)
        if kind is OutputIntentKind.SAFETY_REDUCE:
            target = pending.target_strength
            if (
                target is not None
                and slot.confirmed.strength is not None
                and slot.confirmed.strength <= target
            ):
                pending = replace(pending, target_strength=None)
        slot.pending = pending
        self._refresh_priority(slot)

    @staticmethod
    def _rejection(
        slot: _ChannelSlot,
        started_generation: int,
        kind: OutputIntentKind,
    ) -> TransportOutcome | None:
        if started_generation != slot.generation:
            return _failure("stale output generation")
        if kind < slot.minimum_priority:
            return _failure("blocked by higher-priority output intent")
        if not slot.confirmed.enabled and kind < OutputIntentKind.CLEAR_OR_DISABLE:
            return _failure("channel is disabled")
        return None

    @staticmethod
    def _raise_priority(slot: _ChannelSlot, kind: OutputIntentKind) -> None:
        if kind > slot.minimum_priority:
            slot.minimum_priority = kind

    @staticmethod
    def _refresh_priority(slot: _ChannelSlot) -> None:
        if slot.estop_latched:
            slot.minimum_priority = OutputIntentKind.ESTOP
        elif slot.pending.clear_required:
            slot.minimum_priority = OutputIntentKind.CLEAR_OR_DISABLE
        elif slot.pending.target_strength is not None:
            slot.minimum_priority = OutputIntentKind.SAFETY_REDUCE
        else:
            slot.minimum_priority = OutputIntentKind.MANUAL

    def _slot(self, channel: str) -> _ChannelSlot:
        if channel not in self._slots:
            raise ValueError("channel must be A or B")
        return self._slots[channel]


async def _await_cleanup(task: asyncio.Task[TransportOutcome]) -> TransportOutcome:
    """Shield coordinator-owned transport cleanup from caller cancellation."""

    caller_cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is None or not current.cancelling():
                raise
            caller_cancelled = True
    outcome = task.result()
    if caller_cancelled:
        raise asyncio.CancelledError
    return outcome


def _failure(error: str | None, *, simulated: bool = False) -> TransportOutcome:
    return TransportOutcome(
        sent=False,
        effective=None,
        simulated=simulated,
        error=error,
    )


def _kind(value: object) -> OutputIntentKind:
    if not isinstance(value, OutputIntentKind):
        raise ValueError("kind must be an OutputIntentKind")
    return value


def _strength(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 200:
        raise ValueError("strength must be in 0..200")
    return value


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string when provided")
    return value


def _enabled(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("enabled must be a boolean")
    return value
