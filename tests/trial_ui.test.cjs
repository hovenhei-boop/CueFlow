// Run with Node's built-in test runner; no npm packages are needed.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/cueflow/static/trial/app.js'), 'utf8');

function runtime(fetch = async () => new Response('{}')) {
  const context = vm.createContext({window: {}, document: {querySelector: () => null},
    fetch, AbortController, setTimeout, clearTimeout});
  vm.runInContext(source, context);
  return context.window.CueFlowUI;
}

for (const status of [400, 401, 403, 404, 408, 413, 429, 500, 502, 503, 504]) {
  test(`HTTP ${status} survives a proxy HTML body`, async () => {
    const ui = runtime(async () => new Response('<html>private proxy diagnostic</html>', {status}));
    await assert.rejects(ui.requestJson('/trial/jobs'), error => {
      assert.equal(error.status, status);
      assert.equal(error.message.includes('private proxy'), false);
      assert.equal(error.message.includes('JSON'), false);
      if (status === 413) assert.match(error.message, /上传内容过大/);
      return true;
    });
  });
}
test('structured errors keep the reason-specific Chinese message', async () => {
  const ui = runtime(async () => new Response(JSON.stringify({error: {code: 'visitor_daily_quota', message: 'raw'}}), {status: 429}));
  await assert.rejects(ui.requestJson('/trial/jobs'), /今日试用次数已用完/);
});
test('empty, malformed or non-object successful response is uncertain', async () => {
  for (const body of ['', '<html>gateway</html>', 'null', '[]', '42']) {
    const ui = runtime(async () => new Response(body, {status: 202}));
    await assert.rejects(ui.requestJson('/trial/jobs', {method: 'POST'}), e => e.uncertain === true);
  }
});
test('network failure is uncertain and never resends a POST', async () => {
  let calls = 0;
  const ui = runtime(async () => { calls++; throw new TypeError('private network detail'); });
  await assert.rejects(ui.requestJson('/trial/jobs', {method: 'POST'}), e => e.uncertain === true);
  assert.equal(calls, 1);
});
test('missing and short durations remain distinct from zero', () => {
  const ui = runtime();
  assert.equal(ui.duration(null), '时长待确认');
  assert.equal(ui.duration(undefined), '时长待确认');
  assert.equal(ui.duration(42000), '42 秒');
  assert.equal(ui.duration(83000), '1 分 23 秒');
  assert.equal(ui.duration(500), '不足 1 秒');
  assert.equal(ui.duration(0), '0 秒');
  assert.equal(ui.money(null), '—');
  assert.equal(ui.money(0), '¥0.00');
  assert.equal(ui.percent(null), '—');
});
test('review is terminal presentation, and unknown keys cannot inherit object properties', () => {
  const ui = runtime();
  assert.match(ui.stateInfo('needs_review').note, /暂不支持在线复核/);
  assert.equal(ui.stateInfo('toString').kind, 'unknown');
  assert.equal(ui.stateInfo('__proto__').kind, 'unknown');
  assert.equal(typeof ui.formatTrialError({code: '__proto__', status: 503}), 'string');
});
test('media validation rejects multiple files and the exclusive size boundary', () => {
  const ui = runtime();
  assert.match(ui.validateMedia([{size: 1}, {size: 1}]), /不会自动取第一个/);
  assert.match(ui.validateMedia([{size: 0}]), /文件为空/);
  assert.match(ui.validateMedia([{size: 500000000}]), /小于 500 MB/);
  assert.equal(ui.validateMedia([{size: 499999999}]), '');
});

function clock() {
  let next = 1;
  const timers = new Map(), listeners = new Set();
  const visibility = {visibilityState: 'visible',
    addEventListener: (_, fn) => listeners.add(fn), removeEventListener: (_, fn) => listeners.delete(fn)};
  return {timers, listeners, visibility,
    setTimer: (fn, ms) => { const id = next++; timers.set(id, {fn, ms}); return id; },
    clearTimer: id => timers.delete(id),
    hide() { visibility.visibilityState = 'hidden'; for (const fn of listeners) fn(); },
    show() { visibility.visibilityState = 'visible'; for (const fn of listeners) fn(); },
    tick() { const entries = [...timers.entries()]; for (const [id, timer] of entries) { timers.delete(id); timer.fn(); } }
  };
}
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
test('hidden startup stays silent, returning visible synchronizes once', async () => {
  const ui = runtime(), time = clock();
  let calls = 0;
  time.hide();
  const poller = ui.createVisiblePoller(async () => { calls++; }, 15000, time);
  await poller.start();
  assert.equal(calls, 0); assert.equal(time.timers.size, 0);
  time.show(); await flush();
  assert.equal(calls, 1); assert.equal(time.timers.size, 1);
  assert.equal([...time.timers.values()][0].ms, 15000);
  time.hide(); time.tick(); await flush();
  assert.equal(calls, 1); assert.equal(time.timers.size, 0);
  time.show(); await flush();
  assert.equal(calls, 2); assert.equal(time.timers.size, 1);
  poller.stop();
  assert.equal(time.listeners.size, 0); assert.equal(time.timers.size, 0);
});
test('slow requests, repeated visibility changes and manual refresh never overlap', async () => {
  const ui = runtime(), time = clock();
  let calls = 0, resolve;
  const poller = ui.createVisiblePoller(() => { calls++; return new Promise(done => { resolve = done; }); }, 60000, time);
  const request = poller.start(); await flush();
  for (let i = 0; i < 20; i++) { time.hide(); time.show(); poller.start(); poller.refresh(); }
  await flush(); assert.equal(calls, 1); assert.equal(time.listeners.size, 1); assert.equal(time.timers.size, 0);
  resolve(); await request; await flush();
  assert.equal(time.timers.size, 1);
  time.tick(); await flush(); assert.equal(calls, 2);
  time.hide(); resolve(); await flush(); assert.equal(time.timers.size, 0);
  poller.stop();
});
test('fresh refresh waits for the old request then retrieves post-mutation data', async () => {
  const ui = runtime(), time = clock();
  const resolvers = []; let calls = 0;
  const poller = ui.createVisiblePoller(() => { calls++; return new Promise(done => resolvers.push(done)); }, 15000, time);
  poller.start(); await flush();
  const fresh = poller.refresh(true); assert.equal(calls, 1);
  resolvers.shift()(); await flush(); assert.equal(calls, 2);
  resolvers.shift()(); await fresh; assert.equal(time.timers.size, 1);
  poller.stop();
});
test('failed polling recovers on the next visible tick', async () => {
  const ui = runtime(), time = clock(); let calls = 0;
  const poller = ui.createVisiblePoller(async () => { calls++; throw new Error('offline'); }, 15000, time);
  await poller.start(); assert.equal(time.timers.size, 1);
  time.tick(); await flush(); assert.equal(calls, 2);
  poller.stop();
});
