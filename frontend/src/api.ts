import type {
  BackendFullState,
  BackendReplaySummary,
  BackendRunnerState,
  BackendSessionState,
  ChannelCycleState,
  ChatResult,
  FullState,
  ManualResult,
  NetworkInfo,
  NovelSessionState,
  ReaderTextSlice,
  ReplayHistoryItem,
  StoryAnalysisDetail,
  StoryChapterSummary,
  StoryFinishResult,
  StorySourceSummary,
  TimelineSessionState,
} from "./types";

export function mapStoryError(code: unknown): string {
  switch (code) {
    case "analysis_missing":
      return "尚未导入匹配的离线分析";
    case "analysis_invalid":
      return "离线分析无效，请重新生成并导入";
    case "reader_range_invalid":
      return "无法读取当前正文片段";
    case "story_not_found":
    case "story_reader_missing":
      return "当前小说不可用，请重新导入";
    case "chapter_plan_failed":
      return "章节规划未完成，无法启动阅读";
    case "story_runtime_busy":
    case "story_planning_active":
      return "小说会话正在处理中";
    default:
      return "小说操作失败";
  }
}

export function storyErrorMessage(error: unknown): string {
  const text = error instanceof Error ? error.message : "";
  return Object.values({
    a: mapStoryError("analysis_missing"), b: mapStoryError("analysis_invalid"), c: mapStoryError("reader_range_invalid"),
    d: mapStoryError("story_not_found"), e: mapStoryError("chapter_plan_failed"), f: mapStoryError("story_runtime_busy"),
  }).includes(text) ? text : mapStoryError(undefined);
}

async function j<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, init);
  if (!resp.ok) {
    let msg = `${resp.status} ${resp.statusText}`;
    try {
      const data = (await resp.json()) as { code?: unknown; error?: string };
      if (path.startsWith("/api/story/") || path === "/api/story/import") {
        msg = mapStoryError(data.code);
      } else if (data?.error) msg = data.error;
    } catch {
      /* 无 JSON 错误体时用状态码提示 */
    }
    throw new Error(msg);
  }
  return (await resp.json()) as T;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

const emptyChannel = () => ({
  phase: "idle" as const,
  pattern: null,
  strength: 0,
  cycleIndex: 0,
  nextCycleAtMs: null,
});

function mapPhase(
  phase: unknown,
  status: TimelineSessionState["status"],
): ChannelCycleState["phase"] {
  if (phase === "cycle" || phase === "gap" || phase === "idle") return phase;
  if (phase === "stopped") return status === "paused" ? "paused" : "stopped";
  return status === "paused" ? "paused" : "idle";
}

function mapChannel(
  runner: BackendRunnerState | undefined,
  status: TimelineSessionState["status"],
): ChannelCycleState {
  if (!runner) return { ...emptyChannel(), phase: status === "paused" ? "paused" : "idle" };
  return {
    phase: mapPhase(runner.phase, status),
    pattern: typeof runner.pattern === "string" ? runner.pattern : null,
    strength: typeof runner.strength === "number" ? runner.strength : 0,
    cycleIndex: typeof runner.cycle_index === "number" ? runner.cycle_index : 0,
    nextCycleAtMs:
      typeof runner.next_cycle_start_ms === "number" ? runner.next_cycle_start_ms : null,
  };
}

function mapStatus(status: unknown): TimelineSessionState["status"] {
  if (status === "running" || status === "paused" || status === "finishing" || status === "replaying") {
    return status;
  }
  return "idle";
}

/** Translate the backend's domain-native session/runners once, at the frontend boundary. */
export function mapTimelineState(
  session: BackendSessionState | undefined,
  runners?: Partial<Record<"A" | "B", BackendRunnerState>>,
): TimelineSessionState {
  const status = mapStatus(session?.status);
  const mode = session?.mode === "autopilot" || session?.mode === "replay" ? session.mode : null;
  return {
    sessionId: typeof session?.session_id === "string" ? session.session_id : null,
    status,
    mode,
    cursor: typeof session?.cursor === "number" ? session.cursor : 0,
    adjusted: session?.adjusted === true,
    channels: {
      A: mapChannel(session?.channels?.A ?? runners?.A, status),
      B: mapChannel(session?.channels?.B ?? runners?.B, status),
    },
  };
}

export function mapFullState(raw: BackendFullState): FullState {
  const { session, runners, ...state } = raw;
  return { ...state, timeline: mapTimelineState(session, runners) };
}

function mapReplaySummary(raw: BackendReplaySummary): ReplayHistoryItem {
  if (raw.status !== "completed") throw new Error("回放记录状态无效");
  const role = typeof raw.dlc_role === "string" ? raw.dlc_role : "未标注 DLC";
  const profile = typeof raw.dlc_profile === "string" ? raw.dlc_profile : "";
  return {
    replayId: raw.replay_id,
    title: typeof raw.title === "string" && raw.title.trim() ? raw.title : `回放 ${raw.replay_id.slice(0, 8)}`,
    completedAt: typeof raw.completed_at === "string" ? raw.completed_at : null,
    dlc: profile ? `${role} · ${profile}` : role,
    cycleCount: typeof raw.cycle_count === "number" && raw.cycle_count >= 0 ? raw.cycle_count : 0,
    status: "completed",
    exact: raw.adjusted !== true,
  };
}

export const api = {
  state: () => j<BackendFullState>("/api/state").then(mapFullState),
  chat: (message: string) => j<ChatResult>("/api/chat", json({ message })),
  manual: (action: Record<string, unknown>) =>
    j<ManualResult>("/api/manual", json(action)),
  estop: () => j<ManualResult>("/api/estop", json({})),
  resume: () => j<ManualResult>("/api/resume", json({})),
  clearHistory: () => j<{ ok: boolean }>("/api/history/clear", json({})),
  network: () => j<NetworkInfo>("/api/network"),
  deviceChannels: (
    channels: Record<string, { name?: string; location?: string; baseline?: number }>,
  ) => j<{ ok: boolean }>("/api/device/channels", json(channels)),
  setChannelEnabled: (channel: "A" | "B", enabled: boolean) =>
    j<{ ok: boolean }>("/api/device/channels/enabled", json({ channel, enabled })),
  setChannelCap: (channel: "A" | "B", value: number) =>
    j<{ ok: boolean; user_caps?: Record<string, number> }>(
      "/api/device/channels/cap",
      json({ channel, value }),
    ),
  reportLayout: (body: { sidebar_w: number; control_w: number; inner_width: number; zoom: number }) =>
    j<{ ok: boolean; layout?: Record<string, number> }>("/api/layout", json(body)),
  setSensor: (key: "camera" | "audio", enabled: boolean) =>
    j<{ ok: boolean; sensors?: { camera: boolean; audio: boolean } }>(
      "/api/sensors",
      json({ [key]: enabled }),
    ),
  setProfile: (role: string, profile: string) =>
    j<{ ok: boolean; role?: string; profile?: string }>(
      "/api/character/profile",
      json({ role, profile }),
    ),
  setNick: (nick: string) =>
    j<{ ok: boolean }>("/api/character/nick", json({ nick })),
  importDlc: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return j<{
      ok: boolean;
      dir?: string;
      files?: string[];
      role?: string | null;
      profile?: string | null;
    }>("/api/dlc/import", { method: "POST", body: fd });
  },
  storyImport: (file: File, encoding: StorySourceSummary["encoding"]) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("encoding", encoding);
    return j<{ source: StorySourceSummary; analysis: StoryAnalysisDetail }>("/api/story/import", {
      method: "POST",
      body: fd,
    });
  },
  storyAnalysis: (sourceId: string) =>
    j<StoryAnalysisDetail>(`/api/story/${encodeURIComponent(sourceId)}/analysis`),
  storyChapters: (sourceId: string) =>
    j<{ source: StorySourceSummary; analysis: StoryAnalysisDetail; chapters: StoryChapterSummary[] }>(
      `/api/story/${encodeURIComponent(sourceId)}/chapters`,
    ),
  storyPlay: (sourceId: string, chapterId: string, speed: "slow" | "standard" | "fast") =>
    j<NovelSessionState>(
      `/api/story/${encodeURIComponent(sourceId)}/chapters/${encodeURIComponent(chapterId)}/play`,
      json({ speed }),
    ),
  storyReader: () =>
    j<{ source: StorySourceSummary; analysis: StoryAnalysisDetail; session: NovelSessionState }>(
      "/api/story/reader",
    ),
  storyReaderText: (start: number, end: number) =>
    j<ReaderTextSlice>(`/api/story/reader/text?start=${start}&end=${end}`),
  storyPause: () => j<NovelSessionState>("/api/story/pause", json({})),
  storyResume: (from: "current" | "chapter_start" | "beginning") =>
    j<NovelSessionState>("/api/story/resume", json({ from })),
  storyFinish: () => j<StoryFinishResult>("/api/story/finish", json({})),
  setAutopilot: (enabled: boolean) =>
    j<{ ok: boolean }>("/api/autopilot", json({ enabled })),
  timelineStart: () =>
    j<BackendSessionState>("/api/session/start", json({})).then((session) => mapTimelineState(session)),
  timelinePause: () =>
    j<BackendSessionState>("/api/session/pause", json({})).then((session) => mapTimelineState(session)),
  timelineResume: () =>
    j<BackendSessionState>("/api/session/resume", json({})).then((session) => mapTimelineState(session)),
  timelineFinish: () =>
    j<BackendReplaySummary>("/api/session/finish", json({})).then(mapReplaySummary),
  replays: () => j<BackendReplaySummary[]>("/api/replays").then((items) => items.map(mapReplaySummary)),
  replayPlay: (replayId: string) =>
    j<BackendSessionState>(`/api/replays/${encodeURIComponent(replayId)}/play`, json({})).then((session) => mapTimelineState(session)),
  replayPause: () =>
    j<BackendSessionState>("/api/replays/playback/pause", json({})).then((session) => mapTimelineState(session)),
  replayResume: () =>
    j<BackendSessionState>("/api/replays/playback/resume", json({})).then((session) => mapTimelineState(session)),
  replayStop: () =>
    j<BackendSessionState>("/api/replays/playback/stop", json({})).then((session) => mapTimelineState(session)),
  replayDownloadUrl: (replayId: string) => `/api/replays/${encodeURIComponent(replayId)}/download`,
  getLlm: () =>
    j<{
      base_url: string;
      model: string;
      api_key_masked: string;
      has_key: boolean;
      saved: boolean;
    }>("/api/settings/llm"),
  setLlm: (body: { api_key: string; base_url: string; model: string }) =>
    j<{ ok: boolean; model?: string }>("/api/settings/llm", json(body)),
  testLlm: (body: { api_key: string; base_url: string; model: string }) =>
    j<{ ok: boolean; error?: string; detail?: string }>(
      "/api/settings/llm/test",
      json(body),
    ),
};
