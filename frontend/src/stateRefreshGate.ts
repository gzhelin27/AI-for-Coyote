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
