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
  upload(file: File, durationMs: number, signal: AbortSignal, progress: (percentage: number) => void): Promise<VideoSource> {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const abort = () => xhr.abort();
      const cleanup = () => signal.removeEventListener('abort', abort);
      xhr.open('POST', '/api/video/sources');
      xhr.responseType = 'json';
      xhr.upload.onprogress = event => { if (event.lengthComputable) progress(Math.round(event.loaded / event.total * 100)); };
      xhr.onload = () => {
        cleanup();
        if (xhr.status >= 200 && xhr.status < 300 && typeof xhr.response?.source_id === 'string') resolve(xhr.response as VideoSource);
        else reject(new Error(errorText(xhr.response, `视频上传失败 (${xhr.status})`)));
      };
      xhr.onerror = () => { cleanup(); reject(new Error('视频上传失败，请检查连接')); };
      xhr.onabort = () => { cleanup(); reject(new DOMException('已取消上传', 'AbortError')); };
      signal.addEventListener('abort', abort, { once: true });
      if (signal.aborted) { cleanup(); reject(new DOMException('已取消上传', 'AbortError')); return; }
      const form = new FormData(); form.append('file', file); form.append('duration_ms', String(durationMs));
      xhr.send(form);
    });
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
