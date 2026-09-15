import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
const code = ts.transpileModule(readFileSync(new URL('../src/videoClock.ts', import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
const mod = { exports: {} }; new Function('module', 'exports', code)(mod, mod.exports);
const { VideoClockBridge } = mod.exports;

function harness() {
  const sent = []; let time = 0;
  const bridge = new VideoClockBridge({ sessionId: 'test', send: message => sent.push(message), now: () => time });
  return { sent, bridge, time: value => { time = value; } };
}
test('seeking advances epoch once and paused seek does not play', () => {
  const { sent, bridge } = harness();
  bridge.onMediaState('playing', 1000);
  bridge.onMediaState('seeking', 685000);
  bridge.onMediaState('seeking', 686000);
  bridge.onMediaState('paused', 686000);
  assert.deepEqual(sent.map(m => m.epoch), [1, 2, 2, 2]);
  assert.deepEqual(sent.map(m => m.sequence), [1, 2, 3, 4]);
  assert.equal(sent.at(-1).state, 'paused');
  assert.ok(sent.every(m => m.session_id === 'test' && m.rate === 1));
});
test('playing observations use 10 Hz throttle while state transitions send immediately', () => {
  const { sent, bridge, time } = harness();
  bridge.onMediaState('playing', 0);
  time(99); bridge.onVideoFrame(99); assert.equal(sent.length, 1);
  time(100); bridge.onVideoFrame(100); assert.equal(sent.length, 2);
  time(110); bridge.onMediaState('waiting', 110); assert.equal(sent.length, 3);
  time(500); bridge.onVideoFrame(500); assert.equal(sent.length, 3);
  bridge.onMediaState('paused', 110);
  time(900); bridge.onVideoFrame(900); assert.equal(sent.length, 4);
});
test('close sends a final pause and prevents further observations', () => {
  const { sent, bridge, time } = harness();
  bridge.onMediaState('playing', 500);
  bridge.close(); bridge.close(); time(1000); bridge.onVideoFrame(1500);
  bridge.onMediaState('playing', 1500);
  assert.equal(sent.length, 2); assert.equal(sent.at(-1).state, 'paused');
});
test('invalid positions never refresh playback lease', () => {
  const { bridge, sent } = harness();
  for (const value of [NaN, Infinity, -1]) bridge.onMediaState('playing', value);
  assert.equal(sent.length, 0);
  bridge.onMediaState('playing', 100.8);
  assert.equal(sent[0].position_ms, 100);
});
test('server epoch retires playing heartbeats until explicit user playback', () => {
  const { bridge, sent, time } = harness();
  bridge.onMediaState('playing', 0);
  bridge.syncEpoch(3); time(1000); bridge.onVideoFrame(1000);
  assert.equal(sent.length, 1);
  bridge.syncEpoch(2); bridge.onMediaState('playing', 1000);
  assert.equal(sent.at(-1).epoch, 3); assert.equal(sent.at(-1).sequence, 2);
});
