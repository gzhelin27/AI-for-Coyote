export type VideoMediaState = 'playing' | 'paused' | 'seeking' | 'waiting' | 'ended' | 'error';
export interface VideoChannel {
  target: number; capped_target: number; strength: number;
  pattern: string | null; ramping: boolean; reason: string | null;
}
export interface VideoState {
  session_id: string | null; source_id: string | null;
  status: 'idle' | VideoMediaState; epoch: number; position_ms: number;
  row: { start_ms: number; end_ms: number } | null;
  block: { start_ms: number; end_ms: number; index: number } | null;
  channels: { A: VideoChannel; B: VideoChannel }; error: string | null; dry_run: boolean;
}
export interface VideoObservation {
  session_id: string; epoch: number; sequence: number;
  position_ms: number; state: VideoMediaState; rate: 1;
}
export interface VideoSource { source_id: string; filename: string; duration_ms: number; sha256: string; size: number }

const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' ? value as Record<string, unknown> : {};
const integer = (value: unknown, fallback = 0): number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : fallback;
const nullableText = (value: unknown): string | null => typeof value === 'string' ? value : null;
function channel(value: unknown): VideoChannel {
  const row = record(value);
  return { target: integer(row.target), capped_target: integer(row.capped_target), strength: integer(row.strength),
    pattern: nullableText(row.pattern), ramping: row.ramping === true, reason: nullableText(row.reason) };
}
function interval(value: unknown): { start_ms: number; end_ms: number } | null {
  const row = record(value);
  if (integer(row.start_ms, -1) < 0 || integer(row.end_ms, -1) <= integer(row.start_ms, -1)) return null;
  return { start_ms: row.start_ms as number, end_ms: row.end_ms as number };
}
export function normalizeVideoState(value: unknown): VideoState {
  const data = record(value), channels = record(data.channels);
  const statuses = ['idle', 'playing', 'paused', 'seeking', 'waiting', 'ended', 'error'];
  const valid = statuses.includes(String(data.status));
  const activeValid = data.status !== 'playing' || (typeof data.session_id === 'string' && integer(data.epoch) > 0 &&
    integer(data.position_ms, -1) >= 0 && ['A', 'B'].every(key => {
      const item = record(channels[key]);
      return ['target', 'capped_target', 'strength'].every(field => integer(item[field], -1) >= 0 && integer(item[field]) <= 200);
    }));
  const status = value == null ? 'idle' : valid && activeValid ? data.status as VideoState['status'] : 'error';
  const block = interval(data.block);
  return { session_id: nullableText(data.session_id), source_id: nullableText(data.source_id), status,
    epoch: Math.max(1, integer(data.epoch, 1)), position_ms: integer(data.position_ms), row: interval(data.row),
    block: block ? { ...block, index: integer(record(data.block).index) } : null,
    channels: { A: channel(channels.A), B: channel(channels.B) },
    error: nullableText(data.error) ?? (status === 'error' ? '视频状态无效或输出已停止' : null), dry_run: data.dry_run === true };
}
export function videoTimecode(milliseconds: number): string {
  const seconds = Math.floor(Number.isFinite(milliseconds) ? Math.max(0, milliseconds) / 1000 : 0);
  return `${Math.floor(seconds / 3600)}:${String(Math.floor(seconds / 60) % 60).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}
