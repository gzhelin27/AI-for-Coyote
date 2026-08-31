"""Serialize device output and publish only transport-confirmed channel state."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import IntEnum
from types import MappingProxyType
from typing import TypeAlias, TypeVar


_CHANNELS = ("A", "B")
_EMPTY_EFFECTIVE: Mapping[str, object] = MappingProxyType({})
_NO_PENDING_EXPECTATION = object()
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
    normal_epoch: int = 0
    helper_generation: int = 0
    revision: int = 0
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
        slot.revision += 1

    def revision(self, channel: str) -> int:
        """Return the monotonic confirmed-output revision for one channel."""
        return self._slot(channel).revision

    def helper_generation(self, channel: str) -> int:
        """Return the owner generation for legacy unbounded resend helpers."""
        return self._slot(channel).helper_generation

    def normal_policy_epoch(self, channel: str) -> int:
        """Return the safety-policy epoch observed by normal output work."""
        return self._slot(channel).normal_epoch

    async def confirm_reported_strength(
        self,
        channel: str,
        strength: int,
        *,
        expected_revision: int | None = None,
    ) -> ConfirmedChannelOutput:
        """Order an authoritative report with transport on the channel lock.

        ``expected_revision`` makes a locally mirrored snapshot conditional, so
        it cannot overwrite transport that settled after the snapshot was read.
        """
        slot = self._slot(channel)
        reported_strength = _strength(strength)
        if reported_strength is None:
            raise ValueError("reported strength cannot be None")
        if expected_revision is not None and (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ValueError("expected revision must be a non-negative integer")
        task = asyncio.create_task(
            self._confirm_reported_strength_locked(
                slot, reported_strength, expected_revision
            )
        )
        return await _await_cleanup(task)

    async def confirm_loop_stopped(
        self,
        channel: str,
        waveform: str,
        *,
        expected_revision: int,
        batch_expires_at: float,
    ) -> int | None:
        """Conditionally publish that a repeating waveform owner has stopped.

        A successfully delivered batch remains physical until its deadline, so
        the state becomes ``finite`` while that batch is active and clear after
        it expires.  The revision comparison prevents an older worker from
        overwriting a replacement that committed while cleanup was queued.
        """
        slot = self._slot(channel)
        expected_waveform = _optional_text(waveform, "waveform")
        if expected_waveform is None:
            raise ValueError("waveform cannot be None")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ValueError("expected revision must be a non-negative integer")
        if isinstance(batch_expires_at, bool) or not isinstance(
            batch_expires_at, (int, float)
        ):
            raise ValueError("batch expiry must be a monotonic timestamp")
        expiry = float(batch_expires_at)
        task = asyncio.create_task(
            self._confirm_loop_stopped_locked(
                slot,
                expected_waveform,
                expected_revision,
                expiry,
            )
        )
        return await _await_cleanup(task)

    @staticmethod
    async def _confirm_reported_strength_locked(
        slot: _ChannelSlot,
        strength: int,
        expected_revision: int | None,
    ) -> ConfirmedChannelOutput:
        async with slot.lock:
            if (
                expected_revision is not None
                and slot.revision != expected_revision
            ):
                return slot.confirmed
            # A report supersedes legacy unbounded resend helpers without
            # invalidating the live/replay owner generation for this channel.
            slot.helper_generation += 1
            if slot.confirmed.strength == strength:
                return slot.confirmed
            slot.confirmed = replace(slot.confirmed, strength=strength)
            slot.revision += 1
            return slot.confirmed

    @staticmethod
    async def _confirm_loop_stopped_locked(
        slot: _ChannelSlot,
        waveform: str,
        expected_revision: int,
        batch_expires_at: float,
    ) -> int | None:
        async with slot.lock:
            if slot.revision != expected_revision:
                return None
            if (
                slot.confirmed.waveform != waveform
                or slot.confirmed.waveform_mode != "loop"
            ):
                return None
            batch_active = time.monotonic() < batch_expires_at
            slot.confirmed = replace(
                slot.confirmed,
                waveform=waveform if batch_active else None,
                waveform_mode="finite" if batch_active else None,
            )
            slot.revision += 1
            return slot.revision

    def generation(self, channel: str) -> int:
        return self._slot(channel).generation

    def is_current(self, channel: str, generation: int) -> bool:
        return self._slot(channel).generation == generation

    def invalidate(
        self, channel: str, minimum_priority: OutputIntentKind
    ) -> int:
        slot = self._slot(channel)
        kind = _kind(minimum_priority)
        if kind < slot.minimum_priority:
            return slot.generation
        slot.generation += 1
        if kind > slot.minimum_priority:
            slot.minimum_priority = kind
        if kind is OutputIntentKind.ESTOP:
            slot.estop_latched = True
        return slot.generation

    def invalidate_queued_normal(self, channel: str) -> None:
        """Reject lower-priority work that prepared under an older policy."""
        self._slot(channel).normal_epoch += 1

    def require_clear(self, channel: str) -> None:
        slot = self._slot(channel)
        slot.pending = replace(slot.pending, clear_required=True)
        self.invalidate(channel, OutputIntentKind.CLEAR_OR_DISABLE)

    def mark_reduction(self, channel: str, target: int) -> None:
        slot = self._slot(channel)
        requested_target = _strength(target)
        current_target = slot.pending.target_strength
        strictest_target = (
            requested_target
            if current_target is None
            else min(current_target, requested_target)
        )
        slot.pending = replace(slot.pending, target_strength=strictest_target)
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
        started_normal_epoch = slot.normal_epoch
        pending_expectation: object = _NO_PENDING_EXPECTATION
        if (
            intent is OutputIntentKind.SAFETY_REDUCE
            and slot.pending.target_strength is not None
        ):
            pending_expectation = slot.pending.target_strength
        task = asyncio.create_task(
            self._run_channel_locked(
                slot,
                started_generation,
                started_normal_epoch,
                intent,
                pending_expectation,
                operation,
            )
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

    async def release_estop(self) -> bool:
        """Release both estop latches only after a confirmed global clear."""
        task = asyncio.create_task(self._release_estop_locked())
        return await _await_cleanup(task)

    async def _release_estop_locked(self) -> bool:
        slot_a = self._slots["A"]
        slot_b = self._slots["B"]
        async with slot_a.lock:
            async with slot_b.lock:
                for slot in (slot_a, slot_b):
                    confirmed = slot.confirmed
                    pending = slot.pending
                    if (
                        confirmed.strength != 0
                        or confirmed.waveform is not None
                        or confirmed.waveform_mode is not None
                        or pending.clear_required
                        or pending.target_strength is not None
                    ):
                        return False
                for slot in (slot_a, slot_b):
                    if slot.estop_latched:
                        slot.estop_latched = False
                        slot.generation += 1
                    self._refresh_priority(slot)
                return True

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
        started_normal_epoch: int,
        kind: OutputIntentKind,
        pending_expectation: object,
        operation: _ChannelOperation,
    ) -> TransportOutcome:
        async with slot.lock:
            rejection = self._rejection(
                slot,
                started_generation,
                kind,
                pending_expectation,
                started_normal_epoch,
            )
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
                channel_effective: dict[str, Mapping[str, object]] = {}
                for channel in _CHANNELS:
                    state = effective.get(channel)
                    if not isinstance(state, Mapping) or not self._valid_effective(
                        kind, state
                    ):
                        return _failure(
                            "global transport did not confirm both channels",
                            simulated=outcome.simulated,
                        )
                    channel_effective[channel] = state
                for channel in _CHANNELS:
                    self._commit_sent(
                        self._slots[channel], kind, channel_effective[channel]
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
        effective = outcome.effective or _EMPTY_EFFECTIVE
        if not self._valid_effective(kind, effective):
            return _failure(
                "transport did not confirm effective output state",
                simulated=outcome.simulated,
            )
        self._commit_sent(slot, kind, effective)
        return outcome

    @staticmethod
    def _valid_effective(
        kind: OutputIntentKind, effective: Mapping[str, object]
    ) -> bool:
        try:
            changes = DeviceOutputCoordinator._effective_changes(effective)
        except ValueError:
            return False
        if not changes:
            return False
        if kind >= OutputIntentKind.CLEAR_OR_DISABLE:
            return (
                effective.get("strength") == 0
                and not isinstance(effective.get("strength"), bool)
                and "waveform" in effective
                and effective["waveform"] is None
                and "waveform_mode" in effective
                and effective["waveform_mode"] is None
            )
        return True

    @staticmethod
    def _effective_changes(
        effective: Mapping[str, object],
    ) -> dict[str, object]:
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
        return changes

    def _commit_sent(
        self,
        slot: _ChannelSlot,
        kind: OutputIntentKind,
        effective: Mapping[str, object],
    ) -> None:
        changes = self._effective_changes(effective)
        if changes:
            slot.confirmed = replace(slot.confirmed, **changes)
        slot.revision += 1

        pending = slot.pending
        if kind >= OutputIntentKind.CLEAR_OR_DISABLE:
            pending = replace(
                pending,
                target_strength=None,
                clear_required=False,
            )
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
        pending_expectation: object = _NO_PENDING_EXPECTATION,
        started_normal_epoch: int | None = None,
    ) -> TransportOutcome | None:
        if started_generation != slot.generation:
            return _failure("stale output generation")
        if (
            kind < OutputIntentKind.SAFETY_REDUCE
            and started_normal_epoch is not None
            and started_normal_epoch != slot.normal_epoch
        ):
            return _failure("stale output safety policy")
        if (
            pending_expectation is not _NO_PENDING_EXPECTATION
            and slot.pending.target_strength != pending_expectation
        ):
            return _failure("pending safety reduction changed")
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


_CleanupResult = TypeVar("_CleanupResult")


async def _await_cleanup(task: asyncio.Task[_CleanupResult]) -> _CleanupResult:
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
