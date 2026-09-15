import type { VideoMediaState, VideoObservation } from './videoTypes';

/** Sends observations only; media time never advances from this class's clock. */
export class VideoClockBridge {
  private epoch = 1;
  private sequence = 0;
  private state: VideoMediaState = 'paused';
  private position = 0;
  private lastSent = -Infinity;
  private closed = false;
  constructor(private readonly options: { sessionId: string; send: (message: VideoObservation) => void; now: () => number }) {}
  get currentEpoch(): number { return this.epoch; }
  syncEpoch(epoch: number): void {
    if (Number.isSafeInteger(epoch) && epoch > this.epoch) {
      this.epoch = epoch;
      this.state = 'paused';
    }
  }
  onMediaState(state: VideoMediaState, positionMs: number): void {
    if (this.closed || !Number.isFinite(positionMs) || positionMs < 0) return;
    if (state === 'seeking' && this.state !== 'seeking') this.epoch += 1;
    this.state = state;
    this.position = Math.floor(positionMs);
    this.emit();
  }
  onVideoFrame(positionMs: number): void {
    if (this.closed || this.state !== 'playing' || this.options.now() - this.lastSent < 100 || !Number.isFinite(positionMs) || positionMs < 0) return;
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
