import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const HTML_PATH = path.join(ROOT, 'skills', 'create-blender-story-video', 'assets', 'render-monitor-dashboard', 'index.html');
const html = fs.readFileSync(HTML_PATH, 'utf8');

test('all inline dashboard scripts parse', () => {
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  assert.ok(scripts.length > 0);
  for (const script of scripts) new vm.Script(script[1]);
});

test('polling never overlaps requests or schedules duplicate timers', async () => {
  const start = html.indexOf('    async function fetchStatus()');
  const end = html.indexOf('    function demoShots', start);
  assert.ok(start >= 0 && end > start);
  const functionSource = html.slice(start, end);
  let nextTimer = 0;
  const timers = new Map();
  let fetchCalls = 0;
  let releaseFetch;
  const context = {
    AbortController,
    Date,
    console,
    document: {hidden: false},
    elements: {},
    renderSummary() {},
    setSource() {},
    window: {
      setTimeout(callback) {
        const id = ++nextTimer;
        timers.set(id, callback);
        return id;
      },
      clearTimeout(id) {
        timers.delete(id);
      },
    },
    fetch() {
      fetchCalls++;
      return new Promise((resolve) => { releaseFetch = resolve; });
    },
  };
  vm.createContext(context);
  vm.runInContext(`
    let lastGoodSummary = null;
    let lastGoodAt = 0;
    let pollTimer = 0;
    let pollInFlight = false;
    ${functionSource}
    globalThis.fetchStatus = fetchStatus;
  `, context);

  const first = context.fetchStatus();
  const duplicate = context.fetchStatus();
  assert.equal(fetchCalls, 1);
  releaseFetch({ok: true, json: async () => ({schema_version: 1})});
  await Promise.all([first, duplicate]);
  assert.equal(timers.size, 1);

  const next = context.fetchStatus();
  const nextDuplicate = context.fetchStatus();
  assert.equal(fetchCalls, 2);
  releaseFetch({ok: true, json: async () => ({schema_version: 1})});
  await Promise.all([next, nextDuplicate]);
  assert.equal(timers.size, 1);
});
