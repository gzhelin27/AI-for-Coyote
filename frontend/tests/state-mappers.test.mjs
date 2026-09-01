import assert from "node:assert/strict";
import test from "node:test";

import { api, mapStoryError, mapTimelineState, storyErrorMessage } from "../src/api.ts";
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

test("initial and reconnect fallback HTTP requests belong to their new realtime epoch", () => {
  const gate = new StateSyncGate();
  const initial = gate.beginRealtimeEpoch();
  const initialHttp = gate.beginHttpRequest();
  assert.equal(gate.shouldApplyHttp(initialHttp, 0), true);
  const staleHttp = gate.beginHttpRequest();
  const reconnect = gate.beginRealtimeEpoch();
  const reconnectHttp = gate.beginHttpRequest();
  assert.equal(gate.shouldApplyHttp(staleHttp, 1), false);
  assert.equal(gate.shouldApplyHttp(reconnectHttp, 0), true);
  assert.equal(gate.shouldApplyRealtime(initial, 2), false);
  assert.equal(gate.shouldApplyRealtime(reconnect, 1), true);
});

test("story failures map backend codes to stable reader messages", () => {
  const expected = {
    analysis_missing: "尚未导入匹配的离线分析",
    analysis_invalid: "离线分析无效，请重新生成并导入",
    reader_range_invalid: "无法读取当前正文片段",
    story_not_found: "当前小说不可用，请重新导入",
    story_reader_missing: "当前小说不可用，请重新导入",
    chapter_plan_failed: "章节规划未完成，无法启动阅读",
    story_runtime_busy: "小说会话正在处理中",
    story_planning_active: "小说会话正在处理中",
    story_state_changed: "小说状态已变化，请重新选择章节",
    story_planning_cancelled: "章节规划已取消，请重试",
    story_import_invalid: "小说原文无效，请确认格式和编码",
    story_import_failed: "小说导入未完成，请稍后重试",
    story_output_failed: "设备输出未确认，小说会话未继续",
    story_transition_invalid: "小说会话当前无法切换",
    story_transition_failed: "小说会话当前无法切换",
  };
  for (const [code, message] of Object.entries(expected)) {
    assert.equal(mapStoryError(code), message, code);
  }
  assert.equal(mapStoryError("unexpected_server_detail"), "小说操作失败");
});

test("story API errors preserve every mapped safe message without exposing backend detail", async () => {
  const previousFetch = globalThis.fetch;
  const cases = [
    ["story_output_failed", "设备输出未确认，小说会话未继续"],
    ["story_state_changed", "小说状态已变化，请重新选择章节"],
    ["story_planning_cancelled", "章节规划已取消，请重试"],
  ];
  try {
    for (const [code, expected] of cases) {
      globalThis.fetch = async () => ({
        ok: false,
        status: 409,
        statusText: "Conflict",
        json: async () => ({ code, error: "unsafe backend detail" }),
      });
      const error = await api.storyPause().catch((cause) => cause);
      assert.equal(storyErrorMessage(error), expected, code);
    }
    globalThis.fetch = async () => ({
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
      json: async () => ({ code: "unknown_story_code", error: "unsafe backend detail" }),
    });
    const unknown = await api.storyPause().catch((cause) => cause);
    assert.equal(storyErrorMessage(unknown), "小说操作失败");
  } finally {
    globalThis.fetch = previousFetch;
  }
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

test("reader slice invalidation rejects late active-to-inactive results and resets only on scene identity changes", () => {
  const gate = new ReaderSliceGate();
  const lastPage = gate.begin("later:16384:20000");
  const earlier = gate.begin("earlier:0:8192");
  assert.equal(gate.isCurrent(lastPage, "later:16384:20000"), false);
  assert.equal(gate.isCurrent(earlier, "earlier:0:8192"), true);
  assert.equal(gate.resetPageStart("earlier", 0, 16384), 0);
  assert.equal(gate.resetPageStart("earlier", 0, 8192), null);
  gate.invalidate();
  assert.equal(gate.isCurrent(earlier, "earlier:0:8192"), false);
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
