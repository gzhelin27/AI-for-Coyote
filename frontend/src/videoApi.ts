import { normalizeVideoState, type VideoSource, type VideoState } from './videoTypes';

function errorText(body: unknown, fallback: string): string {
  if (body && typeof body === 'object' && 'detail' in body && typeof body.detail === 'string') return body.detail;
  return fallback;
}
async function request(path: string, options?: RequestInit): Promise<unknown> {
  const response = await fetch(path, options);
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error(errorText(body, `视频请求失败 (${response.status})`));
  return body;
}

export const videoApi = {
  async registerLocal(file: File, durationMs: number, signal: AbortSignal): Promise<VideoSource> {
    return await request('/api/video/local-sources', { method: 'POST', signal,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: file.name, size: file.size, duration_ms: durationMs, last_modified: file.lastModified }) }) as VideoSource;
  },
  async bindCsv(sourceId: string, file: File, signal: AbortSignal): Promise<{ csv_sha256: string; row_count: number }> {
    const form = new FormData(); form.append('file', file);
    return await request(`/api/video/sources/${encodeURIComponent(sourceId)}/csv`, { method: 'POST', body: form, signal }) as { csv_sha256: string; row_count: number };
  },
  async createSession(sourceId: string, signal: AbortSignal): Promise<VideoState> {
    return normalizeVideoState(await request('/api/video/sessions', { method: 'POST', signal,
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source_id: sourceId }) }));
  },
  async state(): Promise<VideoState> { return normalizeVideoState(await request('/api/video/state')); },
  async stop(sessionId: string): Promise<void> {
    await request(`/api/video/sessions/${encodeURIComponent(sessionId)}/stop`, { method: 'POST', keepalive: true });
  },
  socket(sessionId: string): WebSocket {
    return new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/video/sessions/${encodeURIComponent(sessionId)}/clock`);
  },
};
