import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';
const require = createRequire(import.meta.url);
function load(path, imports = {}) {
  const code = ts.transpileModule(readFileSync(new URL(path, import.meta.url), 'utf8'), { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
  const mod = { exports: {} }; new Function('require', 'module', 'exports', code)(name => imports[name] ?? require(name), mod, mod.exports); return mod.exports;
}
const types = load('../src/videoTypes.ts'), clock = load('../src/videoClock.ts');
const flush = async () => { for (let n = 0; n < 20; n++) await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { resolve, reject, promise }; };
const source = { source_id: 'source-1', filename: 'synthetic.mp4', duration_ms: 70000, size: 10, sha256: 'a'.repeat(64) };
const initial = () => ({ ...types.normalizeVideoState(null), session_id: 'session-1', source_id: source.source_id, status: 'paused' });

// Production TSX event handlers with controlled React hook storage and media/transport.
// DOM layout and actual codec support remain the integration browser gate.
function harness(overrides = {}) {
  const previous = { window: globalThis.window, document: globalThis.document, WebSocket: globalThis.WebSocket };
  globalThis.window = { setTimeout: () => 1, clearTimeout() {} };
  globalThis.document = { hidden: false, addEventListener() {}, removeEventListener() {} };
  globalThis.WebSocket = { OPEN: 1 };
  const calls = [], listeners = new Map(); let pauseCount = 0;
  const video = { paused: true, seeking: false, readyState: 4, currentTime: 0, duration: 70, playbackRate: 1,
    pause() { if (!this.paused) { this.paused = true; pauseCount++; listeners.get('pause')?.(); } },
    addEventListener(name, callback) { listeners.set(name, callback); }, removeEventListener(name) { listeners.delete(name); },
    requestVideoFrameCallback() { return 1; }, cancelVideoFrameCallback() {} };
  const socket = { readyState: 0, sent: [], send(value) { this.sent.push(JSON.parse(value)); }, close() { this.readyState = 3; this.onclose?.(); } };
  const api = { async upload(...args) { calls.push(['upload', ...args]); return source; },
    async bindCsv(...args) { calls.push(['csv', ...args]); return { csv_sha256: 'b'.repeat(64), row_count: 1 }; },
    async createSession(...args) { calls.push(['create', ...args]); return initial(); },
    async stop(id) { calls.push(['stop', id]); }, socket() { return socket; }, ...overrides };
  let slots = [], cursor = 0, effects = [], tree, active = true, late = 0;
  const hooks = {
    useState(value) { const index = cursor++; if (!(index in slots)) slots[index] = typeof value === 'function' ? value() : value;
      return [slots[index], value => { if (!active) late++; slots[index] = typeof value === 'function' ? value(slots[index]) : value; }]; },
    useRef(value) { const index = cursor++; if (!(index in slots)) slots[index] = { current: value }; return slots[index]; },
    useEffect(effect, deps) { const index = cursor++, old = slots[index]; if (!old || deps.some((value, n) => !Object.is(value, old.deps[n]))) effects.push(() => { old?.cleanup?.(); slots[index] = { deps, cleanup: effect() }; }); },
  };
  const component = load('../src/components/VideoPlayer.tsx', { react: hooks, '../videoApi': { videoApi: api }, '../videoClock': clock, '../videoTypes': types }).default;
  const nodes = node => node == null ? [] : Array.isArray(node) ? node.flatMap(nodes) : typeof node === 'object' ? [node, ...nodes(node.props?.children)] : [];
  const text = node => node == null ? '' : Array.isArray(node) ? node.map(text).join('') : typeof node === 'object' ? text(node.props?.children) : String(node);
  const render = () => { cursor = 0; tree = component(); nodes(tree).find(node => node.type === 'video').props.ref.current = video; const pending = effects; effects = []; pending.forEach(effect => effect()); };
  render();
  const input = (label, file) => { render(); const node = nodes(tree).find(node => node.props?.['aria-label'] === label); assert.ok(node); assert.ok(!node.props.disabled); node.props.onChange({ target: { files: [file], value: file.name } }); };
  const h = { calls, socket, video, late: () => late, pauseCount: () => pauseCount,
    async select() { input('选择本地视频', new File(['synthetic'], 'synthetic.mp4')); await flush(); render(); },
    async metadata() { render(); await nodes(tree).find(node => node.type === 'video').props.onLoadedMetadata(); await flush(); render(); },
    async csv() { input('导入强度 CSV', new File(['synthetic csv'], 'intensity.csv')); await flush(); render(); },
    async click(label) { render(); const node = nodes(tree).find(node => node.type === 'button' && text(node) === label); assert.ok(node); assert.ok(!node.props.disabled, `${label} enabled`); node.props.onClick(); await flush(); render(); },
    event(name) { listeners.get(name)?.(); },
    open() { socket.readyState = 1; socket.onopen(); render(); },
    receive(state) { socket.onmessage({ data: JSON.stringify(state) }); render(); },
    text() { render(); return text(tree); },
    unmount() { active = false; slots.forEach(slot => slot?.cleanup?.()); },
    restore() { if (active) h.unmount(); Object.assign(globalThis, previous); },
  };
  return h;
}
async function prepared(h) { await h.select(); await h.metadata(); await h.csv(); await h.click('3. 准备播放'); h.open(); }

test('import never plays; user playback sends clock and backend epoch pauses without resume', async () => {
  const h = harness(); try {
    await prepared(h); assert.equal(h.video.paused, true); assert.equal(h.socket.sent.length, 0);
    h.video.paused = false; h.event('playing'); assert.equal(h.socket.sent.at(-1).state, 'playing');
    h.receive({ ...initial(), epoch: 2 }); assert.equal(h.video.paused, true);
    assert.equal(h.socket.sent.at(-1).state, 'paused'); assert.equal(h.socket.sent.at(-1).epoch, 2);
    h.receive({ ...initial(), status: 'playing', epoch: 2 }); assert.equal(h.video.paused, true);
    h.video.paused = false; h.event('playing'); assert.equal(h.socket.sent.at(-1).epoch, 2);
  } finally { h.restore(); }
});
test('paused seek stays paused; fullscreen does not send; non-1 rate is reset', async () => {
  const h = harness(); try {
    await prepared(h); h.video.seeking = true; h.video.currentTime = 25; h.event('seeking');
    h.video.seeking = false; h.event('seeked');
    assert.deepEqual(h.socket.sent.map(m => [m.state, m.epoch]), [['seeking', 2], ['paused', 2]]);
    h.event('fullscreenchange'); assert.equal(h.socket.sent.length, 2);
    h.video.playbackRate = 2; h.event('ratechange'); assert.equal(h.video.playbackRate, 1);
  } finally { h.restore(); }
});
test('socket loss pauses media and requires preparing a new connection', async () => {
  const h = harness(); try {
    await prepared(h); h.video.paused = false; h.event('playing'); h.socket.close(); await flush();
    assert.equal(h.video.paused, true); assert.match(h.text(), /连接已断开/);
    assert.ok(h.calls.some(call => call[0] === 'stop'));
  } finally { h.restore(); }
});
test('late upload after replacement or unmount cannot bind source or update UI', async () => {
  const upload = deferred(); const h = harness({ upload: () => upload.promise });
  try {
    await h.select(); const loading = h.metadata(); await flush(); h.unmount();
    upload.resolve(source); await loading;
    assert.equal(h.late(), 0); assert.equal(h.calls.length, 0);
  } finally { h.restore(); }
});
test('failed CSV keeps prior validated CSV and exposes error without playback', async () => {
  let count = 0; const h = harness({ async bindCsv() { if (++count > 1) throw new Error('区间重叠'); return { csv_sha256: 'b', row_count: 1 }; } });
  try { await h.select(); await h.metadata(); await h.csv(); await h.csv(); assert.match(h.text(), /区间重叠/); assert.match(h.text(), /1 个区间/); assert.equal(h.video.paused, true); }
  finally { h.restore(); }
});
test('failed stop remains retryable before replacing the local source', async () => {
  let stops = 0; const h = harness({ async stop() { if (++stops === 1) throw new Error('清零待确认'); } });
  try {
    await prepared(h); await h.select(); assert.match(h.text(), /清零待确认/);
    await h.select(); assert.equal(stops, 2);
  } finally { h.restore(); }
});
