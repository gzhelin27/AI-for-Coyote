import { api } from "./api";
import { useApp } from "./store";
import type { FullState } from "./types";
import { StateRefreshGate, StateRevisionGate } from "./stateRefreshGate";

const gate = new StateRefreshGate();
const revisionGate = new StateRevisionGate();

export function invalidateStateRefresh(): void {
  gate.invalidate();
}

export function applyRealtimeState(state: FullState): void {
  if (!revisionGate.shouldApply(state.state_revision)) return;
  gate.invalidate();
  useApp.getState().setState(state);
}

export async function refreshAppState(): Promise<FullState | null> {
  const requestGeneration = gate.beginRequest();
  const state = await api.state();
  if (!gate.isCurrent(requestGeneration)) return null;
  useApp.getState().setState(state);
  return state;
}
