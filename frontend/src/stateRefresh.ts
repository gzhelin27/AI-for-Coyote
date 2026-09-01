import { api } from "./api";
import { useApp } from "./store";
import type { FullState } from "./types";
import { StateSyncGate } from "./stateRefreshGate";

const gate = new StateSyncGate();

export function invalidateStateRefresh(): void {
  gate.invalidateHttp();
}

export function beginRealtimeEpoch(): number { return gate.beginRealtimeEpoch(); }
export function isCurrentRealtimeEpoch(epoch: number): boolean { return gate.isCurrentRealtimeEpoch(epoch); }

export function applyRealtimeState(state: FullState, epoch: number): void {
  if (!gate.shouldApplyRealtime(epoch, state.state_revision)) return;
  useApp.getState().setState(state);
}

export async function refreshAppState(): Promise<FullState | null> {
  const requestGeneration = gate.beginHttpRequest();
  const state = await api.state();
  if (!gate.shouldApplyHttp(requestGeneration, state.state_revision)) return null;
  useApp.getState().setState(state);
  return state;
}
