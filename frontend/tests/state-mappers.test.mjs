import assert from "node:assert/strict";
import test from "node:test";

import { mapStoryError, mapTimelineState } from "../src/api.ts";
import { StateRefreshGate, StateRevisionGate } from "../src/stateRefreshGate.ts";

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

test("a lower WebSocket state revision cannot replace an applied newer snapshot", () => {
  const gate = new StateRevisionGate();

  assert.equal(gate.shouldApply(7), true);
  assert.equal(gate.shouldApply(6), false);
  assert.equal(gate.shouldApply(8), true);
});

test("story failures map backend codes to stable reader messages", () => {
  assert.equal(mapStoryError("analysis_missing"), "尚未导入匹配的离线分析");
  assert.equal(mapStoryError("analysis_invalid"), "离线分析无效，请重新生成并导入");
  assert.equal(mapStoryError("reader_range_invalid"), "无法读取当前正文片段");
  assert.equal(mapStoryError("unexpected_server_detail"), "小说操作失败");
});
