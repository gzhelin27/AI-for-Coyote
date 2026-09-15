import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
const code = ts.transpileModule(readFileSync(new URL('../src/videoTypes.ts', import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
const mod = { exports: {} }; new Function('module', 'exports', code)(mod, mod.exports);
const { normalizeVideoState, videoTimecode } = mod.exports;
test('absent state is stopped with independent zero channels', () => {
  const state = normalizeVideoState(null);
  assert.equal(state.status, 'idle'); assert.equal(state.session_id, null);
  assert.equal(state.channels.A.strength, 0); assert.equal(state.channels.B.strength, 0);
  assert.notEqual(state.channels.A, state.channels.B);
});
test('state preserves requested capped and confirmed values and pause reason', () => {
  const state = normalizeVideoState({ session_id: 's', status: 'paused', epoch: 2, position_ms: 620000,
    source_id: 'src', row: { start_ms: 620000, end_ms: 690000 }, block: { start_ms: 620000, end_ms: 650000, index: 0 },
    channels: { A: { target: 60, capped_target: 40, strength: 10, pattern: 'wave', ramping: true, reason: 'cap' }, B: { target: 0, capped_target: 0, strength: 0, pattern: null, ramping: false, reason: null } }, dry_run: true });
  assert.equal(state.channels.A.target, 60); assert.equal(state.channels.A.capped_target, 40);
  assert.equal(state.channels.A.strength, 10); assert.equal(state.channels.A.ramping, true);
  assert.equal(state.row.start_ms, 620000); assert.equal(state.dry_run, true);
});
test('malformed active state fails closed and timecode supports hours above 23', () => {
  assert.equal(normalizeVideoState({ session_id: 's', status: 'playing' }).status, 'error');
  assert.equal(normalizeVideoState({ status: 'unknown' }).status, 'error');
  assert.equal(videoTimecode(620000), '0:10:20'); assert.equal(videoTimecode(90001000), '25:00:01');
  assert.equal(videoTimecode(NaN), '0:00:00');
});
