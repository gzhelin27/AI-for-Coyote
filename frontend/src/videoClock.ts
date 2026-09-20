import type { VideoMediaState, VideoObservation } from './videoTypes';

/** Sends observations only; media time never advances from this class's clock. */
export class VideoClockBridge {
  private epoch = 1;
  private sequence = 0;
  private state: VideoMediaState = 'paused';
  private seeking = false;
  private position = 0;
  private lastSent = -Infinity;
  private closed = false;
  constructor(private readonly options: { sessionId: string; send: (message: VideoObservation) => void; now: () => number }) {}
  get currentEpoch(): number { return this.epoch; }
  get currentSequence(): number { return this.sequence; }
  syncEpoch(epoch: number): void {
    if (Number.isSafeInteger(epoch) && epoch > this.epoch) {
      this.epoch = epoch;
      this.state = 'paused';
      this.seeking = false;
    }
  }
  onMediaState(state: VideoMediaState, positionMs: number): void {
    if (this.closed || !Number.isFinite(positionMs) || positionMs < 0) return;
    if (state === 'playing' && this.state === 'playing' && Math.floor(positionMs) <= this.position) return;
    if (state === 'seeking') {
      if (!this.seeking) this.epoch += 1;
      this.seeking = true;
    } else if (state !== 'waiting') this.seeking = false;
    this.state = state;
    this.position = Math.floor(positionMs);
    this.emit();
  }
  onVideoFrame(positionMs: number): void {
    if (this.closed || this.state !== 'playing' || this.options.now() - this.lastSent < 100 || !Number.isFinite(positionMs) || positionMs < 0) return;
    // Neither rendered frames nor fallback samples can renew a frozen clock.
    if (Math.floor(positionMs) <= this.position) return;
    this.position = Math.floor(positionMs);
    this.emit();
  }
  close(): void {
    if (this.closed) return;
    this.state = 'paused';
    this.emit();
    this.closed = true;
  }
  private emit(): void {
    this.lastSent = this.options.now();
    this.options.send({ session_id: this.options.sessionId, epoch: this.epoch, sequence: ++this.sequence,
      position_ms: this.position, state: this.state, rate: 1 });
  }
}
