"""Media-clock ownership with a watchdog independent of transport ACK waits."""
from __future__ import annotations

import asyncio
import logging
import math
import secrets
import time

from .csv_timeline import find_interval
from .waveforms import find_block

logger = logging.getLogger('ai-for-coyote.video')


class VideoSession:
    LEASE_S = 1.0

    def __init__(self, plan, output, *, source_id, duration_ms, timeline,
                 clock=time.monotonic, dry_run=True, session_id=None, watch=True):
        self.plan, self.output, self.timeline = plan, output, timeline
        self.source_id, self.duration_ms = source_id, duration_ms
        self.clock, self.dry_run = clock, dry_run
        self.session_id = session_id or secrets.token_urlsafe(24)
        self.epoch, self.sequence, self.position_ms = 1, 0, 0
        self.status, self.error, self.closed = 'paused', None, False
        self._watch_enabled, self._watcher, self._work = watch, None, None
        self._revision, self._dirty, self._clear_required = 0, False, False
        self._observed_at, self._lease_until = clock(), 0.
        self._last_clear_attempt = -math.inf
        self._wave_keys = {'A': None, 'B': None}
        self._wave_until = {'A': 0., 'B': 0.}
        self._boundary_cleared = None
        self._close_task = None
        self.audit = []

    async def start(self):
        await self.output.clear()
        self.output.bind_session(self)
        if not self.output.claim():
            raise RuntimeError('video output ownership changed during startup')
        if self._watch_enabled:
            self._watcher = asyncio.create_task(self._watch(), name='video-lease-watchdog')
        return self.state()

    def state(self):
        row = find_interval(self.timeline, self.position_ms)
        block = find_block(self.plan, self.position_ms)
        channels = {}
        for c in ('A', 'B'):
            snap = self.output.snapshot(c)
            target = getattr(row, f'{c.lower()}_target') if row else 0
            pattern = getattr(block, f'{c.lower()}_pattern') if block else None
            capped = min(target, snap.cap)
            reason = (self.error or ('清零待确认' if self._clear_required else
                      '未覆盖区间' if row is None else
                      '已停止' if self.status != 'playing' else
                      '安全上限' if target > snap.cap else
                      '等待输出确认' if snap.strength != capped else None))
            channels[c] = {'target': target, 'capped_target': capped, 'strength': snap.strength,
                           'pattern': pattern if self.status == 'playing' and target else None,
                           'reason': reason}
        return {'session_id': self.session_id, 'source_id': self.source_id,
                'status': self.status, 'epoch': self.epoch, 'sequence': self.sequence,
                'position_ms': self.position_ms,
                'row': None if row is None else {'start_ms': row.start_ms, 'end_ms': row.end_ms},
                'block': None if block is None else {'start_ms': block.start_ms, 'end_ms': block.end_ms, 'index': block.index},
                'channels': channels, 'error': self.error, 'dry_run': self.dry_run,
                'clear_pending': self._clear_required or self.output.clearing}

    async def observe(self, observation):
        if self.closed:
            return self.state()
        if not isinstance(observation, dict):
            raise ValueError('invalid video observation')
        if observation.get('session_id') != self.session_id:
            raise ValueError('wrong video session')
        epoch, sequence = observation.get('epoch'), observation.get('sequence')
        position, status = observation.get('position_ms'), observation.get('state')
        if (type(epoch) is not int or type(sequence) is not int or sequence <= 0 or
                type(position) is not int or not 0 <= position <= self.duration_ms or
                status not in ('playing', 'paused', 'seeking', 'waiting', 'ended', 'error') or
                type(observation.get('rate', 1)) not in (int, float) or observation.get('rate', 1) != 1):
            raise ValueError('invalid video time, state or playback rate')
        if sequence <= self.sequence:
            return self.state()
        if epoch != self.epoch and not (status == 'seeking' and epoch == self.epoch + 1):
            return self.state()
        now = self.clock()
        if self.status == 'playing' and now >= self._lease_until and status == 'playing':
            self._invalidate('paused', '播放时钟已失联', bump_epoch=True)
            return self.state()
        old_block = find_block(self.plan, self.position_ms)
        next_block = find_block(self.plan, position)
        if status == 'playing' and self.status == 'playing' and old_block != next_block:
            self._revision += 1
            # An in-flight ACK belongs to the old node; quiesce it before a
            # new target can use the possibly uncertain physical strength.
            if self._work is not None and not self._work.done():
                if self.output.inflight_strength or next_block is None:
                    self.output.preempt()
                    self._clear_required = True
                else:
                    self.output.retire_normal()
        self.sequence, self.epoch = sequence, epoch
        self.position_ms = position
        self._observed_at = now
        if status != 'playing':
            self._invalidate(status, '视频播放错误' if status == 'error' else None)
        else:
            # A new observation can authorize only this media location.
            self.status, self.error = 'playing', None
            self._lease_until = now + self.LEASE_S
            self._schedule()
        return self.state()

    def _schedule(self):
        self._dirty = True
        if self._work is None or self._work.done():
            self._work = asyncio.create_task(self._drive(), name='video-output-update')

    def _log_interruption(self, status, error):
        if error and (status, error) != (self.status, self.error):
            logger.warning('Video interrupted: reason=%s status=%s epoch=%s sequence=%s '
                           'position_ms=%s observation_age_ms=%s', error, status,
                           self.epoch, self.sequence, self.position_ms,
                           round(max(0, self.clock() - self._observed_at) * 1000))

    def _invalidate(self, status, error=None, *, bump_epoch=False):
        self._log_interruption(status, error)
        self._revision += 1
        self._boundary_cleared = None
        if bump_epoch:
            self.epoch += 1
        self.status, self.error = status, error
        self._lease_until = 0.
        self._clear_required = True
        # Crucially not behind _work or a transport lock: this wakes ACK waiters.
        self.output.preempt()
        self._schedule()

    async def tick(self):
        now = self.clock()
        if self.status == 'playing':
            if now >= self._lease_until:
                self._invalidate('paused', '播放时钟已失联', bump_epoch=True)
            elif not self.output.owns_control():
                self._invalidate('paused', '输出控制权已改变', bump_epoch=True)
            else:
                block = find_block(self.plan, self.position_ms)
                projected = self.position_ms + max(0, now - self._observed_at) * 1000
                if block and projected >= block.end_ms and self._boundary_cleared != block.index:
                    self._boundary_cleared = block.index
                    self._revision += 1
                    if find_block(self.plan, block.end_ms) is None or self.output.inflight_strength:
                        self.output.preempt()
                        self._clear_required = True
                    else:
                        self.output.retire_normal()
                self._schedule()
        elif self._clear_required and now - self._last_clear_attempt >= .25:
            self._schedule()
        return self.state()

    async def _watch(self):
        try:
            while not self.closed:
                await asyncio.sleep(.025)
                await self.tick()
        except asyncio.CancelledError:
            pass

    def _remaining(self, block, channel, *, append):
        now = self.clock()
        projected = self.position_ms + max(0, now - self._observed_at) * 1000
        deadline = min(self._lease_until, now + max(0, block.end_ms - projected) / 1000)
        start = max(now, self._wave_until[channel]) if append else now
        return max(0, int(round((deadline - start) * 1000)))

    def _authorizes_output(self, actions, generations):
        """Validate executor intent against this live, independently parsed plan."""
        if (self.closed or self.status != 'playing' or self._clear_required
                or not self.output.is_current() or generations != self.output.generations
                or self.clock() >= self._lease_until or len(actions) != 1):
            return False
        block = find_block(self.plan, self.position_ms)
        if block is None:
            return False
        action = actions[0]
        channel = action.get('channel')
        if channel not in ('A', 'B'):
            return False
        snapshot = self.output.snapshot(channel)
        requested = getattr(block, f'{channel.lower()}_target')
        if not requested or snapshot.blocked or not snapshot.enabled:
            return False
        if action.get('op') == 'hold_strength':
            return (self._remaining(block, channel, append=False) > 0
                    and type(action.get('value')) is int
                    and action['value'] == min(requested, snapshot.cap))
        # A queued waveform may reach its block deadline while this owner and
        # lease remain valid. The transport deadline guard discards that stale
        # fragment as a normal boundary, rather than retiring the session.
        return (action.get('op') == 'pulse_video'
                and action.get('pattern') == getattr(block, f'{channel.lower()}_pattern'))

    async def _drive(self):
        while self._dirty:
            self._dirty = False
            revision = self._revision
            if self._clear_required:
                self._last_clear_attempt = self.clock()
                try:
                    await self.output.clear()
                except Exception:
                    if revision != self._revision:
                        self._dirty = True
                        continue
                    self._log_interruption('error', '输出清零未确认')
                    self.status, self.error = 'error', '输出清零未确认'
                    self._dirty = False
                    break
                if revision != self._revision:
                    self._dirty = True
                    continue
                self._clear_required = False
                self._wave_keys = {'A': None, 'B': None}
                self._wave_until = {'A': 0., 'B': 0.}
                if not self.output.claim():
                    if self.status == 'playing':
                        self._invalidate('paused', '输出控制权已改变', bump_epoch=True)
                    continue
            if self.closed or self.status != 'playing':
                continue
            if self.clock() >= self._lease_until:
                self._invalidate('paused', '播放时钟已失联', bump_epoch=True)
                continue
            if not self.output.owns_control():
                self._invalidate('paused', '输出控制权已改变', bump_epoch=True)
                continue
            block = find_block(self.plan, self.position_ms)
            try:
                for channel in ('A', 'B'):
                    if revision != self._revision or self.status != 'playing':
                        break
                    snap = self.output.snapshot(channel)
                    requested = getattr(block, f'{channel.lower()}_target') if block else 0
                    if not requested:
                        if snap.strength or self._wave_keys[channel] is not None:
                            await self.output.clear((channel,))
                            owned = self.output.claim()
                            self._wave_keys[channel], self._wave_until[channel] = None, 0.
                            if revision != self._revision:
                                break
                            if not owned:
                                self._invalidate('paused', '输出控制权已改变', bump_epoch=True)
                                break
                        continue
                    if snap.blocked or not snap.enabled:
                        self._invalidate('paused', '通道安全状态阻止播放', bump_epoch=True)
                        break
                    # Never increase beyond an observed block's safe end.
                    remaining = self._remaining(block, channel, append=False)
                    if remaining <= 0:
                        if find_block(self.plan, block.end_ms) is None and self._boundary_cleared != block.index:
                            self._boundary_cleared = block.index
                            self._clear_required = True
                            self.output.preempt()
                            self._schedule()
                        continue
                    target = min(requested, snap.cap)
                    if target != snap.strength:
                        submitted_at = self.clock()
                        observed_at, observed_position = self._observed_at, self.position_ms
                        receipt = await self.output.set_strength(channel, target,
                            remaining=lambda b=block, c=channel: self._remaining(b, c, append=False))
                        if revision != self._revision:
                            break
                        if not receipt.success:
                            raise RuntimeError(receipt.error)
                        self._record(channel, requested, receipt.strength, block.index,
                                     submitted_at, observed_at, observed_position, snap.cap)
                    if revision != self._revision or self.clock() >= self._lease_until:
                        break
                    pattern = getattr(block, f'{channel.lower()}_pattern')
                    key = (block.index, pattern)
                    if self._wave_keys[channel] != key:
                        receipt = await self.output.replace_block(channel, pattern, self._remaining(block, channel, append=False),
                            remaining=lambda b=block, c=channel: self._remaining(b, c, append=False))
                        if revision != self._revision:
                            break
                        if not receipt.success:
                            raise RuntimeError(receipt.error)
                        self._wave_keys[channel] = key
                        self._wave_until[channel] = self.clock() + receipt.duration_ms / 1000
                    elif self.clock() >= self._wave_until[channel] - .05:
                        receipt = await self.output.continue_block(channel, pattern, self._remaining(block, channel, append=True),
                            remaining=lambda b=block, c=channel: self._remaining(b, c, append=True))
                        if revision != self._revision:
                            break
                        if not receipt.success:
                            raise RuntimeError(receipt.error)
                        self._wave_until[channel] = max(self.clock(), self._wave_until[channel]) + receipt.duration_ms / 1000
            except Exception as exc:
                if revision == self._revision:
                    self._invalidate('error', '设备输出未确认', bump_epoch=True)

    def _record(self, channel, target, strength, block_index, submitted_at,
                observed_at, observed_position, cap):
        self.audit.append({'channel': channel, 'target': target, 'strength': strength,
                           'capped_target': min(target, cap), 'cap': cap,
                           'block': block_index, 'position_ms': observed_position,
                           'observed_at': observed_at, 'submitted_at': submitted_at,
                           'confirmed_at': self.clock()})
        if len(self.audit) > 10000:
            del self.audit[:1000]

    async def flush(self):
        work = self._work
        if work is not None and work is not asyncio.current_task():
            await asyncio.shield(work)

    async def close(self, reason='stopped'):
        if (self._close_task is not None and self._close_task.done()
                and not self._close_task.cancelled() and self._close_task.exception() is not None
                and self._clear_required):
            self._close_task = None
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_cleanup(reason), name='video-close')
        return await asyncio.shield(self._close_task)

    async def _close_cleanup(self, reason):
        self.closed = True
        self._invalidate('ended', bump_epoch=True)
        watcher = self._watcher
        if watcher is not None and watcher is not asyncio.current_task():
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        await self.flush()
        if self._clear_required:
            raise RuntimeError('video output clear was not confirmed')
        return self.state()

    def interrupt(self, reason='stopped'):
        """Retire output synchronously; callers may then await close cleanup."""
        self._invalidate('paused', reason, bump_epoch=True)
