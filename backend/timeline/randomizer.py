"""Seeded, one-time resolution of live plot-event directives."""

from __future__ import annotations

import hashlib
import random
from collections.abc import Mapping, Sequence
from typing import Any

from .models import ChannelDirective, DirectiveMode, PlotEvent


_CHANNELS = ("A", "B")
_STREAMS = frozenset(("plot:A", "plot:B", "cycle:A", "cycle:B"))


def derive_stream_seed(session_seed: int, stream: str) -> int:
    """Derive a stable, independent random seed for a named channel stream."""
    if stream not in _STREAMS:
        raise ValueError("unsupported random stream")
    digest = hashlib.sha256(f"{session_seed}:{stream}".encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


class TimelineResolver:
    """Resolve AI strength intent into deterministic per-channel plot directives."""

    def __init__(self, *, strength_jitter: int, session_seed: int) -> None:
        if isinstance(strength_jitter, bool) or not isinstance(strength_jitter, int) or strength_jitter < 0:
            raise ValueError("strength_jitter must be a non-negative integer")
        if isinstance(session_seed, bool) or not isinstance(session_seed, int):
            raise ValueError("session_seed must be an integer")
        self.strength_jitter = strength_jitter
        self._plot_rngs = {
            channel: random.Random(derive_stream_seed(session_seed, f"plot:{channel}"))
            for channel in _CHANNELS
        }

    def resolve_plot_event(
        self,
        actions: Sequence[Mapping[str, Any]],
        current: Mapping[str, int],
        caps: Mapping[str, int],
        enabled: Mapping[str, bool],
        presets: Sequence[str],
        event_id: str,
        scene_id: str,
        offset_ms: int,
    ) -> PlotEvent:
        """Resolve one plot event without sampling any channel-cycle gaps."""
        active_channels = tuple(channel for channel in _CHANNELS if enabled.get(channel, False))
        if not active_channels:
            raise ValueError("at least one channel must be enabled")

        effective_caps = {channel: self._cap(caps, channel) for channel in active_channels}
        targets = {
            channel: self._clamp(self._number(current.get(channel, 0)), effective_caps[channel])
            for channel in active_channels
        }
        modes = {channel: DirectiveMode.KEEP for channel in active_channels}

        for action in actions:
            if not isinstance(action, Mapping):
                continue
            op = action.get("op")
            if op == "stop":
                for channel in active_channels:
                    modes[channel] = DirectiveMode.STOP
                continue
            if op == "clear":
                requested_channel = action.get("channel")
                clear_channels = active_channels if requested_channel is None else (requested_channel,)
                for channel in clear_channels:
                    if channel in modes:
                        modes[channel] = DirectiveMode.STOP
                continue

            channel = action.get("channel")
            if channel not in targets or modes[channel] is DirectiveMode.STOP:
                continue
            if op in ("hold_strength", "temp_strength"):
                targets[channel] = self._clamp(self._number(action.get("value", 0)), effective_caps[channel])
                modes[channel] = DirectiveMode.SET
            elif op == "add_strength":
                targets[channel] = self._clamp(
                    targets[channel] + self._number(action.get("delta", 0)), effective_caps[channel]
                )
                modes[channel] = DirectiveMode.SET

        set_channels = tuple(channel for channel in active_channels if modes[channel] is DirectiveMode.SET)
        allowed_presets = self._presets(presets) if set_channels else ()
        directives: dict[str, ChannelDirective] = {}
        for channel in active_channels:
            if modes[channel] is DirectiveMode.STOP:
                directives[channel] = ChannelDirective(channel=channel, mode=DirectiveMode.STOP)
            elif modes[channel] is DirectiveMode.SET:
                rng = self._plot_rngs[channel]
                base_strength = targets[channel]
                directives[channel] = ChannelDirective(
                    channel=channel,
                    mode=DirectiveMode.SET,
                    pattern=rng.choice(allowed_presets),
                    base_strength=base_strength,
                    resolved_strength=self._clamp(
                        base_strength + rng.randint(-self.strength_jitter, self.strength_jitter),
                        effective_caps[channel],
                    ),
                )
            else:
                directives[channel] = ChannelDirective(channel=channel, mode=DirectiveMode.KEEP)

        return PlotEvent(
            event_id=event_id,
            scene_id=scene_id,
            offset_ms=offset_ms,
            channels=directives,
        )

    @staticmethod
    def _number(value: object) -> int:
        if isinstance(value, bool):
            return 0
        try:
            return int(float(value))
        except (TypeError, ValueError, OverflowError):
            return 0

    @classmethod
    def _cap(cls, caps: Mapping[str, int], channel: str) -> int:
        cap = cls._number(caps.get(channel, 0))
        return cls._clamp(cap, 200)

    @staticmethod
    def _clamp(value: int, cap: int) -> int:
        return max(0, min(value, cap))

    @staticmethod
    def _presets(presets: Sequence[str]) -> tuple[str, ...]:
        if isinstance(presets, (str, bytes)):
            raise ValueError("presets must be a sequence of names")
        allowed = tuple(pattern for pattern in presets if isinstance(pattern, str) and pattern.strip())
        if not allowed:
            raise ValueError("at least one allowed preset is required for a set directive")
        return allowed
