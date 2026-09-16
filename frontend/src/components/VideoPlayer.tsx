import { useEffect, useRef, useState } from 'react';
import { videoApi } from '../videoApi';
import { VideoClockBridge } from '../videoClock';
import { normalizeVideoState, videoTimecode, type VideoMediaState, type VideoSource } from '../videoTypes';

const statusText = { idle: '未启动', paused: '已暂停', playing: '播放中', seeking: '定位中', waiting: '等待视频', ended: '已结束', error: '已停止' };
const message = (error: unknown) => error instanceof Error ? error.message : '视频操作失败';
type Connection = { sessionId: string; socket: WebSocket; bridge: VideoClockBridge };

export default function VideoPlayer() {
  const media = useRef<HTMLVideoElement>(null);
  const connection = useRef<Connection | null>(null);
  const pendingStop = useRef<string | null>(null);
  const selected = useRef<File | null>(null);
  const objectUrl = useRef('');
  const operation = useRef(0);
  const pending = useRef<AbortController | null>(null);
  const registered = useRef(false);
  const [url, setUrl] = useState('');
  const [source, setSource] = useState<VideoSource | null>(null);
  const [csv, setCsv] = useState<{ csv_sha256: string; row_count: number } | null>(null);
  const [state, setState] = useState(() => normalizeVideoState(null));
  const [position, setPosition] = useState(0);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [ready, setReady] = useState(false);

  async function retire() {
    media.current?.pause();
    const current = connection.current;
    connection.current = null;
    setReady(false);
    setState(previous => ({ ...previous, status: previous.status === 'idle' ? 'idle' : 'paused' }));
    if (current) {
      pendingStop.current = current.sessionId;
      current.bridge.close();
      current.socket.close();
    }
    const stopping = pendingStop.current;
    if (stopping) {
      await videoApi.stop(stopping);
      if (pendingStop.current === stopping) pendingStop.current = null;
    }
  }

  async function chooseFile(file: File) {
    const version = ++operation.current;
    pending.current?.abort();
    setError(''); setBusy('停止旧会话…');
    try {
      await retire();
      if (version !== operation.current) return;
      if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
      selected.current = file;
      registered.current = false;
      objectUrl.current = URL.createObjectURL(file);
      setSource(null); setCsv(null); setState(normalizeVideoState(null)); setPosition(0);
      setBusy('读取视频信息…'); setUrl(objectUrl.current);
    } catch (failure) {
      if (version === operation.current) { setError(message(failure)); setBusy(''); }
    }
  }

  async function loadedMetadata() {
    const video = media.current, file = selected.current;
    if (!video || !file || registered.current) return;
    const duration = Math.floor(video.duration * 1000);
    if (!Number.isSafeInteger(duration) || duration <= 0) { setError('无法读取视频时长，请选择浏览器支持的本地视频'); setBusy(''); return; }
    registered.current = true;
    const version = operation.current, controller = new AbortController();
    pending.current = controller;
    setBusy('准备本地视频…');
    try {
      const result = await videoApi.registerLocal(file, duration, controller.signal);
      if (version === operation.current) setSource(result);
    } catch (failure) {
      if (version === operation.current) { registered.current = false; setError(message(failure)); }
    } finally { if (version === operation.current) setBusy(''); }
  }

  async function importCsv(file: File) {
    if (!source) return;
    const version = ++operation.current;
    pending.current?.abort();
    const controller = new AbortController(); pending.current = controller;
    setBusy('校验 CSV…'); setError('');
    try {
      await retire();
      if (version !== operation.current) return;
      const result = await videoApi.bindCsv(source.source_id, file, controller.signal);
      if (version === operation.current) { setCsv(result); setState(normalizeVideoState(null)); }
    } catch (failure) { if (version === operation.current) setError(message(failure)); }
    finally { if (version === operation.current) setBusy(''); }
  }

  async function prepare() {
    if (!source || !csv) return;
    const version = ++operation.current;
    setBusy('准备会话…'); setError('');
    try {
      await retire();
      if (version !== operation.current) return;
      // Let creation finish so an unmounted or superseded result can be stopped by its ID.
      const result = await videoApi.createSession(source.source_id, new AbortController().signal);
      if (!result.session_id) throw new Error(result.error ?? '无法创建视频会话');
      if (version !== operation.current) { await videoApi.stop(result.session_id); return; }
      setState(result);
      const socket = videoApi.socket(result.session_id);
      const bridge = new VideoClockBridge({ sessionId: result.session_id, now: () => performance.now(), send: observation => {
        if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(observation));
      } });
      bridge.syncEpoch(result.epoch);
      const current = { sessionId: result.session_id, socket, bridge };
      connection.current = current;
      const timeout = window.setTimeout(() => { if (socket.readyState !== WebSocket.OPEN) socket.close(); }, 5000);
      socket.onopen = () => {
        window.clearTimeout(timeout);
        if (connection.current !== current) { socket.close(); return; }
        setReady(true); setBusy('');
      };
      socket.onmessage = event => {
        if (connection.current !== current) return;
        try {
          const payload = JSON.parse(event.data);
          const incoming = normalizeVideoState(payload);
          if (incoming.session_id !== current.sessionId || incoming.epoch < bridge.currentEpoch) return;
          const advanced = incoming.epoch > bridge.currentEpoch;
          // A delayed echo of an earlier pause/wait cannot undo newer local play.
          // Safety invalidation advances the server epoch and always wins.
          if (!advanced && Number.isSafeInteger(payload.sequence) && payload.sequence >= 0 &&
              payload.sequence < bridge.currentSequence && !incoming.error && incoming.status !== 'error') return;
          bridge.syncEpoch(incoming.epoch);
          setState(incoming);
          if (advanced || ['paused', 'error', 'ended', 'idle'].includes(incoming.status)) {
            media.current?.pause();
          }
          if (incoming.error) setError(incoming.error);
        } catch { media.current?.pause(); bridge.onMediaState('error', Math.floor((media.current?.currentTime ?? 0) * 1000)); setError('视频状态读取失败，已暂停'); }
      };
      socket.onerror = () => { if (connection.current === current) { media.current?.pause(); setError('视频连接失败，请重新准备会话'); } };
      socket.onclose = () => {
        window.clearTimeout(timeout);
        if (connection.current !== current) return;
        media.current?.pause(); bridge.close(); connection.current = null;
        setReady(false); setBusy(''); setError('视频连接已断开，请重新准备会话');
        pendingStop.current = current.sessionId;
        setState(previous => ({ ...previous, status: 'error', error: '视频连接已断开' }));
        void videoApi.stop(current.sessionId).then(() => {
          if (pendingStop.current === current.sessionId) pendingStop.current = null;
        }).catch(() => {});
      };
    } catch (failure) { if (version === operation.current) { setError(message(failure)); setBusy(''); } }
  }

  useEffect(() => {
    const video = media.current;
    if (!video) return;
    let frameId = 0, animationId = 0, stopped = false, lastDisplay = 0;
    const observe = (state: VideoMediaState) => {
      const current = connection.current;
      if (state === 'playing' && (video.paused || !current || current.socket.readyState !== WebSocket.OPEN)) {
        video.pause(); return;
      }
      current?.bridge.onMediaState(state, Math.floor(video.currentTime * 1000));
      setPosition(Math.floor(video.currentTime * 1000));
    };
    const playing = () => observe('playing');
    const paused = () => { if (!video.seeking) observe('paused'); };
    const seeking = () => observe('seeking');
    const seeked = () => observe(video.paused ? 'paused' : video.readyState < 3 ? 'waiting' : 'playing');
    const waiting = () => observe('waiting');
    const ended = () => observe('ended');
    const failed = () => { observe('error'); video.pause(); setBusy(''); setError('无法播放此视频。请尝试浏览器支持的 MP4 或 WebM 文件'); };
    const rate = () => { if (video.playbackRate !== 1) video.playbackRate = 1; };
    const visibility = () => { if (document.hidden) video.pause(); };
    const listeners: [string, () => void][] = [['playing', playing], ['pause', paused], ['seeking', seeking], ['seeked', seeked],
      ['waiting', waiting], ['ended', ended], ['error', failed], ['ratechange', rate]];
    listeners.forEach(([name, listener]) => video.addEventListener(name, listener));
    document.addEventListener('visibilitychange', visibility);
    const sample = () => {
      if (stopped) return;
      const positionMs = Math.floor(video.currentTime * 1000);
      if (!video.paused && !video.seeking) connection.current?.bridge.onVideoFrame(positionMs);
      if (performance.now() - lastDisplay >= 100) { setPosition(positionMs); lastDisplay = performance.now(); }
    };
    if ('requestVideoFrameCallback' in video) {
      const frame = () => { sample(); if (!stopped) frameId = video.requestVideoFrameCallback(frame); };
      frameId = video.requestVideoFrameCallback(frame);
    } else {
      const frame = () => { sample(); if (!stopped) animationId = requestAnimationFrame(frame); };
      animationId = requestAnimationFrame(frame);
    }
    return () => {
      stopped = true;
      listeners.forEach(([name, listener]) => video.removeEventListener(name, listener));
      document.removeEventListener('visibilitychange', visibility);
      if (frameId) video.cancelVideoFrameCallback(frameId);
      if (animationId) cancelAnimationFrame(animationId);
    };
  }, [url]);

  useEffect(() => () => {
    ++operation.current; pending.current?.abort(); media.current?.pause();
    const current = connection.current; connection.current = null;
    if (current) { current.bridge.close(); current.socket.close(); void videoApi.stop(current.sessionId).catch(() => {}); }
    else if (pendingStop.current) void videoApi.stop(pendingStop.current).catch(() => {});
    if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
  }, []);

  return <section className="space-y-4 p-4" aria-label="本地视频播放">
    <div><h2 className="text-lg font-semibold">本地视频</h2><p className="mt-1 text-sm text-muted">视频时间驱动 CSV 强度，支持 1 倍速。暂停、定位或失联会停止输出。</p></div>
    <div className="flex flex-wrap items-end gap-3">
      <label className="block text-sm">1. 选择视频<input aria-label="选择本地视频" type="file" accept="video/*,.mp4,.webm,.mov,.mkv" className="mt-1 block max-w-full text-xs" onChange={event => {
        const file = event.target.files?.[0]; event.target.value = ''; if (file) void chooseFile(file);
      }} /></label>
      <label className="block text-sm">2. 导入强度 CSV<input aria-label="导入强度 CSV" type="file" accept=".csv,text/csv" disabled={!source || !!busy} className="mt-1 block max-w-full text-xs disabled:opacity-40" onChange={event => {
        const file = event.target.files?.[0]; event.target.value = ''; if (file) void importCsv(file);
      }} /></label>
      <button className="rounded-lg border border-line px-3 py-2 text-sm disabled:opacity-40" disabled={!csv || !!busy || ready} onClick={() => void prepare()}>3. 准备播放</button>
      <button className="rounded-lg border border-line px-3 py-2 text-sm disabled:opacity-40" disabled={!ready} onClick={() => {
        ++operation.current; void retire().then(() => setState(previous => ({ ...previous, status: 'paused' }))).catch(failure => setError(message(failure)));
      }}>停止会话</button>
    </div>
    {busy && <p role="status" className="text-sm text-muted">{busy}</p>}
    {error && <p role="alert" className="rounded-lg border border-bad/40 p-3 text-sm text-bad">{error}</p>}
    <video ref={media} src={url || undefined} controls preload="metadata" playsInline onLoadedMetadata={() => void loadedMetadata()}
      className="max-h-[55vh] w-full rounded-xl bg-black" aria-label="本地视频播放器" />
    <p className="text-sm text-muted">{ready ? '会话已就绪，请点击视频播放按钮。' : '选择本地视频并完成 CSV 校验后，准备会话再点击播放。'} {source?.filename} {csv && `· ${csv.row_count} 个区间`}</p>
    <p className="text-xs text-muted">视频不上传，由浏览器直接读取本地文件；仅发送文件信息和 CSV。刷新页面后需重新选择视频与 CSV。</p>
    <div className="flex flex-wrap gap-4 text-sm"><span>视频 {videoTimecode(position)} / {videoTimecode(source?.duration_ms ?? 0)}</span>
      <span>{statusText[state.status]}{state.dry_run ? ' · 模拟输出' : ''}</span>
      <span>区间：{state.row ? `${videoTimecode(state.row.start_ms)}–${videoTimecode(state.row.end_ms)}` : '空档，两路目标为 0'}</span>
    </div>
    <div className="grid gap-3 sm:grid-cols-2">{(['A', 'B'] as const).map(name => {
      const channel = state.channels[name];
      return <div key={name} className="rounded-xl border border-line bg-ink2 p-3">
        <h3 className="font-semibold">通道 {name}</h3><dl className="mt-2 grid grid-cols-3 gap-2 text-sm">
          <div><dt className="text-muted">CSV 目标</dt><dd>{channel.target}</dd></div>
          <div><dt className="text-muted">限幅目标</dt><dd>{channel.capped_target}</dd></div>
          <div><dt className="text-muted">已确认强度</dt><dd>{channel.strength}</dd></div>
        </dl><p className="mt-2 text-sm">波形：{channel.pattern ?? '停止'}</p>
        {channel.target > channel.capped_target && <p className="mt-1 text-xs text-warn">目标受当前安全上限约束。</p>}
        {channel.reason && <p className="mt-1 text-xs text-muted">原因：{channel.reason}</p>}
      </div>;
    })}</div>
    <details className="text-sm text-muted"><summary className="cursor-pointer">CSV 格式与播放规则</summary>
      <pre className="mt-2 overflow-x-auto rounded-lg bg-ink2 p-3">{'start_time,end_time,A_target,B_target\n0:10:20,0:11:30,20,15'}</pre>
      <p className="mt-2">时间使用 H:MM:SS 文本格式；未覆盖区间归零。每行独立划分最多 30 秒的随机波形段，定位后保持已选序列。每次选择视频都需重新绑定 CSV，同名文件不会复用旧绑定；准备完成不会自动播放。</p>
      <p className="mt-2">强度直接到达限幅目标，即 CSV 目标与当前安全上限的较小值；定位后继续播放、暂停后恢复也采用同一规则。实际输出以设备确认为准。</p>
    </details>
  </section>;
}
