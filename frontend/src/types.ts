// ---------- 与后端 /api/state 等接口对齐的类型 ----------
export interface RelayDevice {
  slotId: string;
  name: string;
  type: string;
}
export interface RelayClientInfo {
  clientId: string;
  slotId: string | null;
  devices: RelayDevice[];
  props: Record<string, unknown>;
  slotState: Record<string, unknown>;
}
export interface RelayState {
  status: "disconnected" | "connecting" | "waiting" | "paired" | string;
  controller_id: string | null;
  url: string;
  clients: RelayClientInfo[];
  last_error: string;
}
export interface PresetInfo {
  name: string;
  waveform: string;
  label: string;
  default_duration_s: number;
  max_duration_s: number;
  category: string;
  frames?: string[];
}
export interface ChannelDevice {
  name: string;
  location: string;
}
export interface RoleProfile {
  name: string;
  level: string;
  note: string;
  available: boolean;
}
export interface RoleInfo {
  name: string;
  label: string;
  title: string;
  device_narrative: string;
  profiles: RoleProfile[];
}
export interface FullState {
  state_revision: number;
  estop: boolean;
  caps: Record<"A" | "B", number>;
  user_caps: Record<"A" | "B", number>;
  effective_caps: Record<"A" | "B", number>;
  app_caps: Record<"A" | "B", number | null>;
  current: Record<"A" | "B", number>;
  requested: Record<"A" | "B", number | null>;
  pulse_active: Record<"A" | "B", boolean>;
  overheat: Record<"A" | "B", boolean>;
  device_channels: Record<"A" | "B", ChannelDevice>;
  active_channels: Record<"A" | "B", boolean>;
  enabled_channels: Record<"A" | "B", boolean>;
  baseline_strength: Record<"A" | "B", number>;
  patterns: Record<"A" | "B", string | null>;
  role: string;
  role_title: string;
  roles: RoleInfo[];
  profile: string;
  profiles: string[];
  profile_available: Record<string, boolean>;
  profile_level: string;
  autopilot: boolean;
  autopilot_interval_s: number;
  sensors_on: boolean;
  sensors: { camera: boolean; audio: boolean };
  max_pulse_s: number;
  max_temp_s: number;
  max_step: number;
  presets: PresetInfo[];
  playback: {
    frame_ms: number;
    min_duration_s: number;
    max_duration_s: number;
    loop_batch_s: number;
    loop_overlap_s: number;
  };
  ui: { quick_strengths: number[]; default_temp_s: number; default_pulse_s: number };
  dry_run: boolean;
  relay_status: string;
  controller_id: string | null;
  connected: boolean;
  notes: string[];
  camera_enabled: boolean;
  camera: {
    enabled: boolean;
    has_frame: boolean;
    last_ts: number;
    interval_s: number;
    mean_brightness: number;
    dark: boolean;
    dark_threshold: number;
    error: string;
  };
  audio: {
    enabled: boolean;
    running: boolean;
    last_text: string;
    last_ts: number;
    level: number;
    level_pct?: number;
    threshold: number;
    model_size: string;
    error: string;
  };
  relay: RelayState;
  character: string;
  config_info: {
    model: string;
    safeword: string;
    character_file: string;
    waveforms_file: string;
    title: string;
    profile: string;
    player_nick: string;
    version: string;
  };
  timeline: TimelineSessionState;
  story: StoryState;
}

export type StoryAnalysisStatus = "ready" | "missing" | "invalid";

export interface StorySourceSummary {
  source_id: string;
  filename: string;
  extension: ".txt" | ".md" | ".docx";
  encoding: "auto" | "utf-8" | "gb18030";
  hash_prefix: string;
  text_length: number;
}

export interface StoryAnalysisDetail {
  status: StoryAnalysisStatus;
  hash_prefix: string;
  analysis_version: string;
  dlc_version: string;
}

export interface StoryChapterSummary {
  chapter_id: string;
  index: number;
  title: string;
  summary: string;
  start_offset: number;
  end_offset: number;
  scene_count: number;
}

export type NovelSessionStatus = "idle" | "planning" | "validated" | "running" | "paused" | "finishing";

export interface NovelSessionState {
  status: NovelSessionStatus;
  hash_prefix: string | null;
  filename: string | null;
  chapter_id: string | null;
  speed: "slow" | "standard" | "fast" | null;
  cursor: number;
  current_scene_id: string | null;
  reader_start_offset: number | null;
  reader_end_offset: number | null;
  progress: number;
}

export interface StoryState {
  selected_source: StorySourceSummary | null;
  analysis: StoryAnalysisDetail | null;
  chapters: StoryChapterSummary[];
  session: NovelSessionState;
}

export interface ReaderTextSlice {
  start: number;
  end: number;
  text_length: number;
  text: string;
}

export interface ExecutedItem {
  label: string;
  reason: string;
  sent: boolean;
}
export interface DroppedItem {
  reason: string;
  action?: unknown;
}
export interface ManualResult {
  executed: ExecutedItem[];
  dropped: DroppedItem[];
}
export interface ChatResult extends ManualResult {
  line: string;
  error?: string | null;
}
export interface NetworkInfo {
  lan_ip: string;
  all_ips: string[];
  pair_url: string | null;
  public_url: string;
  relay_port: number;
  hint: string;
}

// ---------- 时间线会话（前端模型） ----------
export interface ChannelCycleState {
  phase: "idle" | "cycle" | "gap" | "paused" | "stopped";
  pattern: string | null;
  strength: number;
  cycleIndex: number;
  nextCycleAtMs: number | null;
}

export interface TimelineSessionState {
  sessionId: string | null;
  status: "idle" | "running" | "paused" | "finishing" | "replaying";
  mode: "autopilot" | "replay" | null;
  cursor: number;
  adjusted: boolean;
  channels: { A: ChannelCycleState; B: ChannelCycleState };
}

/** 后端会话/runner 保持领域原生 snake_case；仅在 api.ts 的边界转换。 */
export interface BackendSessionState {
  session_id?: string | null;
  status?: string;
  mode?: string | null;
  cursor?: number;
  adjusted?: boolean;
  channels?: Partial<Record<"A" | "B", BackendRunnerState>>;
}

export interface BackendRunnerState {
  phase?: string;
  pattern?: string | null;
  strength?: number;
  cycle_index?: number;
  next_cycle_start_ms?: number | null;
}

export type BackendFullState = Omit<FullState, "timeline"> & {
  session?: BackendSessionState;
  runners?: Partial<Record<"A" | "B", BackendRunnerState>>;
};

export interface ReplayHistoryItem {
  replayId: string;
  title: string;
  completedAt: string | null;
  dlc: string;
  cycleCount: number;
  status: "completed";
  exact: boolean;
}

export interface ReplaySummary {
  replay_id: string;
  session_id: string;
  status: string;
  mode: string;
  title: string;
  cycle_count: number;
  created_at: string;
  completed_at: string | null;
  model: string;
  dlc_role: string;
  dlc_profile: string;
  adjusted: boolean;
}

export type BackendReplaySummary = ReplaySummary;

export interface StoryFinishResult {
  replay: ReplaySummary;
  session: NovelSessionState;
}
