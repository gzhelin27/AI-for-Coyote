import { api } from "./api";
import { useApp } from "./store";
import type { FullState } from "./types";
import { StateRefreshGate } from "./stateRefreshGate";

const gate = new StateRefreshGate();

export function invalidateStateRefresh(): void {
  gate.invalidate();
}

export function applyRealtimeState(state: FullState): void {
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
