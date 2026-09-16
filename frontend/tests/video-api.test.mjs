import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
const code = ts.transpileModule(readFileSync(new URL('../src/videoApi.ts', import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
const mod = { exports: {} }; new Function('require', 'module', 'exports', code)(() => ({ normalizeVideoState: value => value }), mod, mod.exports);
const { videoApi } = mod.exports;

test('10 GiB local video registration sends only small metadata JSON and never reads video bytes', async () => {
  const previous = globalThis.fetch, calls = [];
  const file = { name: 'local-large.mp4', size: 10 * 1024 ** 3, lastModified: 123456,
    arrayBuffer() { throw new Error('must not read video'); }, stream() { throw new Error('must not stream video'); } };
  globalThis.fetch = async (path, options) => { calls.push([path, options]); return { ok: true, json: async () => ({ source_id: `local-${calls.length}` }) }; };
  try {
    const controller = new AbortController();
    const first = await videoApi.registerLocal(file, 7200000, controller.signal);
    const second = await videoApi.registerLocal(file, 7200000, controller.signal);
    assert.notEqual(first.source_id, second.source_id);
    assert.equal(calls.length, 2);
    const [path, options] = calls[0];
    assert.equal(path, '/api/video/local-sources'); assert.equal(options.method, 'POST');
    assert.equal(options.signal, controller.signal); assert.equal(options.headers['Content-Type'], 'application/json');
    assert.equal(typeof options.body, 'string'); assert.ok(options.body.length < 256);
    assert.deepEqual(JSON.parse(options.body), { filename: file.name, size: file.size, duration_ms: 7200000, last_modified: file.lastModified });
    assert.equal('upload' in videoApi, false);
  } finally { globalThis.fetch = previous; }
});
test('CSV import continues to send its file using multipart form data', async () => {
  const previous = globalThis.fetch; let request;
  globalThis.fetch = async (path, options) => { request = [path, options]; return { ok: true, json: async () => ({ csv_sha256: 'a', row_count: 1 }) }; };
  try {
    const file = new File(['start_time,end_time,A_target,B_target'], 'intensity.csv');
    await videoApi.bindCsv('local-1', file, new AbortController().signal);
    assert.equal(request[0], '/api/video/sources/local-1/csv');
    assert.ok(request[1].body instanceof FormData); assert.equal(request[1].body.get('file').name, file.name);
  } finally { globalThis.fetch = previous; }
});
