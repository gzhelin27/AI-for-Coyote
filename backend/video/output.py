"""Video output uses existing safety validation and transport ownership."""
from dataclasses import dataclass
import time

from backend.output_coordinator import OutputIntentKind


@dataclass(frozen=True)
class OutputSnapshot:
    strength: int
    cap: int
    max_step: int
    enabled: bool
    blocked: bool


@dataclass(frozen=True)
class OutputReceipt:
    success: bool
    strength: int
    duration_ms: int = 0
    error: str | None = None


class GameLoopVideoOutput:
    CHUNK_MS = 500

    def __init__(self, game_loop, *, clock=time.monotonic):
        self.loop = game_loop
        self.clock = clock
        self.inflight_strength = False
        self.generations = {}
        self.offsets = {'A': 0, 'B': 0}

    def claim(self):
        self.generations = self.loop.begin_timeline_output(('A', 'B'))

    def snapshot(self, channel):
        safety = self.loop.safety
        confirmed = self.loop.output_coordinator.confirmed(channel)
        pending = self.loop.output_coordinator.pending(channel)
        return OutputSnapshot(
            int(confirmed.strength or 0), safety.cap_for(channel), safety.max_step,
            confirmed.enabled and safety.desired_enabled.get(channel, False),
            safety.estop_active or safety.overheat.get(channel, False)
            or pending.clear_required or pending.target_strength is not None)

    def is_current(self):
        return bool(self.generations) and all(
            self.loop.output_coordinator.is_current(c, g) for c, g in self.generations.items())

    def preempt(self, channels=('A', 'B')):
        # Synchronous: wake ACK waiters before any action/session lock is awaited.
        self.loop.require_output_clear(channels)

    def retire_normal(self):
        """Retire a waveform node without resetting known intensity."""
        self.claim()
        self.loop._interrupt_relay_waits(('A', 'B'))

    async def clear(self, channels=('A', 'B')):
        self.preempt(channels)
        result = await self.loop.clear_output(None if set(channels) == {'A', 'B'} else channels[0])
        if not result[0] or result[1]:
            raise RuntimeError('video output clear was not confirmed')
        for channel in channels:
            self.offsets[channel] = 0

    async def _execute(self, channel, action, *, remaining=None):
        if channel not in self.generations or not self.is_current():
            return OutputReceipt(False, self.snapshot(channel).strength, error='retired video output')
        executed, dropped = await self.loop.execute_actions(
            [action], intent=OutputIntentKind.TIMELINE_OR_REPLAY,
            owner_generations=dict(self.generations), waveform_managed_channels=(channel,),
            video_remaining_ms=remaining)
        if dropped or not executed:
            if action.get('op') == 'pulse_video' and dropped and dropped[0].get('reason') == 'video block deadline elapsed':
                return OutputReceipt(True, self.snapshot(channel).strength)
            return OutputReceipt(False, self.snapshot(channel).strength,
                                 error=str(dropped[0].get('reason')) if dropped else 'output failed')
        if not self.is_current():
            return OutputReceipt(False, self.snapshot(channel).strength, error='retired video output')
        return OutputReceipt(True, self.snapshot(channel).strength,
                             int(executed[-1].get('effective', {}).get('duration_ms', 0)))

    async def set_strength(self, channel, target, *, remaining=None):
        self.inflight_strength = True
        try:
            return await self._execute(channel, {'op': 'hold_strength', 'channel': channel, 'value': target},
                                       remaining=remaining)
        finally:
            self.inflight_strength = False

    async def replace_block(self, channel, pattern, remaining_ms, *, remaining=None):
        deadline = self.clock() + remaining_ms / 1000
        remaining = remaining or (lambda: max(0, int((deadline - self.clock()) * 1000)))
        if not self.is_current():
            return OutputReceipt(False, self.snapshot(channel).strength, error='retired video output')
        await self.loop.replace_timeline_waveform(channel)
        self.offsets[channel] = 0
        return await self._chunk(channel, pattern, remaining(), append=False, remaining=remaining)

    async def continue_block(self, channel, pattern, remaining_ms, *, remaining=None):
        deadline = self.clock() + remaining_ms / 1000
        remaining = remaining or (lambda: max(0, int((deadline - self.clock()) * 1000)))
        return await self._chunk(channel, pattern, remaining_ms, append=True, remaining=remaining)

    async def _chunk(self, channel, pattern, remaining_ms, *, append, remaining):
        duration = min(self.CHUNK_MS, max(0, int(remaining_ms))) // 100 * 100
        if duration < 100:
            return OutputReceipt(True, self.snapshot(channel).strength)
        receipt = await self._execute(channel, {
            'op': 'pulse_video', 'channel': channel, 'pattern': pattern,
            'duration_ms': duration, 'frame_offset': self.offsets[channel],
            'append': append, '_strength_prerequisite_confirmed': True}, remaining=remaining)
        if receipt.success:
            self.offsets[channel] += receipt.duration_ms // 100
        return receipt
