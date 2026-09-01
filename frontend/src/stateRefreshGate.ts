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
}
