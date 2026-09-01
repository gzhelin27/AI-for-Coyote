/** Keeps late HTTP state snapshots from overwriting newer state mutations. */
export class StateRefreshGate {
  private generation = 0;

  beginRequest(): number {
    this.generation += 1;
    return this.generation;
  }

  invalidate(): void {
    this.generation += 1;
  }

  isCurrent(requestGeneration: number): boolean {
    return requestGeneration === this.generation;
  }
}

/** Keeps out-of-order full-state WebSocket frames from rewinding public state. */
export class StateRevisionGate {
  private lastApplied = -1;

  shouldApply(stateRevision: number): boolean {
    if (!Number.isFinite(stateRevision) || stateRevision < this.lastApplied) return false;
    this.lastApplied = stateRevision;
    return true;
  }

  reset(): void { this.lastApplied = -1; }
}

export class StateSyncGate {
  private readonly refresh = new StateRefreshGate();
  private readonly revision = new StateRevisionGate();
  private realtimeEpoch = 0;
  beginHttpRequest(): number { return this.refresh.beginRequest(); }
  shouldApplyHttp(requestGeneration: number, revision: number): boolean {
    return this.refresh.isCurrent(requestGeneration) && this.revision.shouldApply(revision);
  }
  beginRealtimeEpoch(): number {
    this.refresh.invalidate();
    this.revision.reset();
    this.realtimeEpoch += 1;
    return this.realtimeEpoch;
  }
  shouldApplyRealtime(epoch: number, revision: number): boolean {
    return epoch === this.realtimeEpoch && this.revision.shouldApply(revision);
  }
  invalidateHttp(): void { this.refresh.invalidate(); }
  isCurrentRealtimeEpoch(epoch: number): boolean { return epoch === this.realtimeEpoch; }
}
