import assert from "node:assert/strict";
import test from "node:test";

import { mapStoryError, mapTimelineState } from "../src/api.ts";
import { ReaderSliceGate, readerPageRange } from "../src/readerPaging.ts";
import { StateSyncGate } from "../src/stateRefreshGate.ts";
import { analysisStatusGuidance, buildMissingAnalysisCommand, isStoryReady, resumePayload } from "../src/storyUi.ts";

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

test("state synchronization rejects stale HTTP and WebSocket snapshots across reconnect epochs", () => {
  const gate = new StateSyncGate();
  const epoch = gate.beginRealtimeEpoch();
  const http = gate.beginHttpRequest();
  assert.equal(gate.shouldApplyHttp(http, 9), true);
  assert.equal(gate.shouldApplyRealtime(epoch, 8), false);
  const oldHttp = gate.beginHttpRequest();
  const nextEpoch = gate.beginRealtimeEpoch();
  assert.equal(gate.shouldApplyRealtime(nextEpoch, 0), true);
  assert.equal(gate.shouldApplyHttp(oldHttp, 10), false);
  assert.equal(gate.shouldApplyRealtime(epoch, 11), false);
});

test("story failures map backend codes to stable reader messages", () => {
  assert.equal(mapStoryError("analysis_missing"), "尚未导入匹配的离线分析");
  assert.equal(mapStoryError("analysis_invalid"), "离线分析无效，请重新生成并导入");
  assert.equal(mapStoryError("reader_range_invalid"), "无法读取当前正文片段");
  assert.equal(mapStoryError("unexpected_server_detail"), "小说操作失败");
});

test("reader pages cover short and long scenes without exceeding the API slice bound", () => {
  assert.deepEqual(readerPageRange(10, 18, null), { start: 10, end: 18, hasPrevious: false, hasNext: false });
  assert.deepEqual(readerPageRange(0, 20000, null), { start: 0, end: 8192, hasPrevious: false, hasNext: true });
  assert.deepEqual(readerPageRange(0, 20000, 8192), { start: 8192, end: 16384, hasPrevious: true, hasNext: true });
  assert.deepEqual(readerPageRange(0, 20000, 16384), { start: 16384, end: 20000, hasPrevious: true, hasNext: false });
});

test("reader slice generation ignores an older success or error after the page changes", () => {
  const gate = new ReaderSliceGate();
  const first = gate.begin("scene-1:0:8192");
  const second = gate.begin("scene-1:8192:16384");
  assert.equal(gate.isCurrent(first, "scene-1:0:8192"), false);
  assert.equal(gate.isCurrent(second, "scene-1:8192:16384"), true);
});

test("offline-analysis guidance is actionable and start/resume controls stay bounded", () => {
  const command = buildMissingAnalysisCommand("a1b2c3d4e5f6", "gb18030");
  assert.match(command, /^\$storyPath = Read-Host '请输入小说完整路径'/);
  assert.match(command, /--source "\$storyPath" --map "data\/story_candidates\/a1b2c3d4e5f6\.json" --encoding gb18030$/);
  assert.equal(command.includes("<"), false);
  assert.equal(isStoryReady("ready"), true);
  assert.equal(isStoryReady("missing"), false);
  assert.deepEqual(resumePayload("current"), { from: "current" });
  assert.deepEqual(resumePayload("chapter_start"), { from: "chapter_start" });
  assert.deepEqual(resumePayload("beginning"), { from: "beginning" });
  assert.equal(analysisStatusGuidance("invalid"), "离线分析无效，请重新生成并导入");
});
