import assert from "node:assert/strict";
import test from "node:test";

import { mapTimelineState } from "../src/api.ts";
import { StateRefreshGate } from "../src/stateRefreshGate.ts";

test("replay session channel state overrides legacy runner fallback", () => {
  const timeline = mapTimelineState(
    {
      status: "replaying",
      mode: "replay",
      cursor: 2,
      channels: {
        A: {
          phase: "gap",
          pattern: "呼吸",
          strength: 17,
          cycle_index: 4,
          next_cycle_start_ms: 2300,
        },
        B: {
          phase: "idle",
          pattern: null,
          strength: 0,
          cycle_index: 0,
          next_cycle_start_ms: null,
        },
      },
    },
    { A: { phase: "idle", strength: 0 } },
  );

  assert.deepEqual(timeline.channels.A, {
    phase: "gap",
    pattern: "呼吸",
    strength: 17,
    cycleIndex: 4,
    nextCycleAtMs: 2300,
  });
});

test("a realtime invalidation rejects an older HTTP refresh generation", () => {
  const gate = new StateRefreshGate();
  const pending = gate.beginRequest();

  gate.invalidate();

  assert.equal(gate.isCurrent(pending), false);
  assert.equal(gate.isCurrent(gate.beginRequest()), true);
});
