// 時間のかかる操作の「受け付け → 問い合わせ」（static/studio_operation.js・2026-10-14）。外部への通信なし。
const {test, beforeEach} = require('node:test');
const assert = require('node:assert/strict');
const operation = require('../static/studio_operation.js');

operation.settings.pollMs = 1;
operation.settings.maxBackoffMs = 2;

function json(status, body) {
  return {ok: status >= 200 && status < 300, status, type: 'basic', json: async () => body, text: async () => JSON.stringify(body)};
}
const redirected = () => ({ok: false, status: 0, type: 'opaqueredirect', json: async () => { throw new Error('opaque'); }});

let calls;
function serve(...responses) {
  calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push({url, init});
    const next = responses.shift();
    if (next === undefined) throw new Error('unexpected request: ' + url);
    if (next instanceof Error) throw next;
    return typeof next === 'function' ? next(url, init) : next;
  };
}
const posts = () => calls.filter(call => call.init.method === 'POST').length;

beforeEach(() => { operation.settings.maxWaitMs = 60000; operation.settings.maxFailures = 20; operation.settings.verifyFailures = 3; });

test('accepted work is polled until done and looks like the old response', async () => {
  const waits = [];
  serve(json(202, {ok: true, operation: {id: 'x'}}), json(200, {status: 'running', waiting: true, elapsed: 1}),
        json(200, {status: 'done', result: {status: 200, body: {ok: true, job_id: 'scene_fix_1'}}}));
  const response = await operation.fetch('/api/scene-fix', {method: 'POST', body: 'form', headers: {'X-Material-CSRF': 't'},
    onWait: state => waits.push(state)});
  assert.equal(response.ok, true);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {ok: true, job_id: 'scene_fix_1'});
  assert.equal(JSON.parse(await response.text()).job_id, 'scene_fix_1');
  const sent = calls[0].init;
  assert.equal(sent.headers['X-Studio-Async'], '1');
  assert.equal(sent.headers['X-Material-CSRF'], 't');
  assert.equal(sent.redirect, 'manual');
  assert.equal('onWait' in sent, false);
  assert.match(sent.headers['X-Studio-Operation'], /^[0-9a-f]{32}$/);
  assert.equal(calls[1].url, '/api/operations/' + sent.headers['X-Studio-Operation']);
  assert.deepEqual(waits, [{elapsed: 1, waiting: true}]);
});

test('errors keep their status code so the pages show the same messages', async () => {
  serve(json(202, {}), json(200, {status: 'done', result: {status: 409, body: {error: 'この案件の素材を処理中です'}}}));
  const response = await operation.fetch('/api/material-jobs/a/variant', {method: 'POST'});
  assert.equal(response.ok, false);
  assert.equal(response.status, 409);
  assert.equal((await response.json()).error, 'この案件の素材を処理中です');
});

test('non-JSON results still give text, and json() fails like fetch', async () => {
  serve(json(202, {}), json(200, {status: 'done', result: {status: 500, text: '<html>Internal Server Error</html>'}}));
  const response = await operation.fetch('/api/regenerate/a/1', {method: 'POST'});
  assert.equal(response.status, 500);
  assert.equal(await response.text(), '<html>Internal Server Error</html>');
  await assert.rejects(response.json());
});

test('old servers and immediate refusals are returned as they are', async () => {
  for (const status of [200, 400, 401, 403, 409, 413, 429]) {
    const original = json(status, {error: 'x'});
    serve(original);
    assert.equal(await operation.fetch('/api/scene-fix', {method: 'POST'}), original);
    assert.equal(calls.length, 1);
  }
});

test('a 5xx on sending is checked with the same number, so pressing again never makes it twice', async () => {
  const gateway = json(502, {error: 'Bad gateway'});
  serve(gateway, json(404, {error: 'not accepted'}));
  assert.equal(await operation.fetch('/api/scene-fix', {method: 'POST'}), gateway);  // 受け付けていない → 今までどおりの失敗
  serve(json(524, {}), json(200, {status: 'running'}), json(200, {status: 'done', result: {status: 200, body: {ok: true}}}));
  const response = await operation.fetch('/api/scene-fix', {method: 'POST'});
  assert.equal(response.status, 200);  // 受け付け済みだった → 結果を待って返す
  assert.equal(posts(), 1);
});

test('expired login (Cloudflare redirect) is reported at once', async () => {
  serve(redirected());
  const first = await operation.fetch('/api/scene-fix', {method: 'POST'});
  assert.equal(first.status, 401);
  assert.match((await first.json()).error, /ログイン/);
  serve(json(202, {}), redirected());
  const later = await operation.fetch('/api/scene-fix', {method: 'POST'});
  assert.equal(later.status, 401);
  assert.equal(calls[1].init.redirect, 'manual');
  const expired = json(401, {error: 'ログインセッションが切れました。'});
  serve(json(202, {}), expired);
  assert.equal(await operation.fetch('/api/scene-fix', {method: 'POST'}), expired);
});

test('interrupted or unreadable work is reported without retrying', async () => {
  for (const status of ['interrupted', 'unknown']) {
    serve(json(202, {}), json(200, {status, error: 'サーバーの再起動で中断しました。'}));
    const response = await operation.fetch('/api/scene-fix', {method: 'POST'});
    assert.equal(response.status, 409);
    assert.match((await response.json()).error, /中断/);
    assert.equal(calls.length, 2);
  }
});

test('a lost acceptance reply is checked with the same number, never sent twice', async () => {
  serve(new TypeError('network'), json(200, {status: 'done', result: {status: 200, body: {ok: true}}}));
  const response = await operation.fetch('/api/scene-fix', {method: 'POST'});
  assert.equal(response.status, 200);
  assert.equal(posts(), 1);
  assert.equal(calls[1].url, '/api/operations/' + calls[0].init.headers['X-Studio-Operation']);
  serve(new TypeError('network'), json(404, {error: 'この操作は受け付けられていません。'}));
  const missing = await operation.fetch('/api/scene-fix', {method: 'POST'});
  assert.equal(missing.status, 503);
  assert.match((await missing.json()).error, /もう一度押して/);
  serve(new TypeError('network'), new TypeError('offline'), new TypeError('offline'), new TypeError('offline'));
  const offline = await operation.fetch('/api/scene-fix', {method: 'POST'});
  assert.equal(offline.status, 503);
  assert.equal(calls.length, 4);  // 確かめるのは3回まで（以前のように長く待たせない）
});

test('network gaps while polling are retried, then give up with guidance', async () => {
  serve(json(202, {}), new TypeError('offline'), json(502, {}), json(200, {status: 'done', result: {status: 200, body: {ok: true}}}));
  assert.equal((await operation.fetch('/api/scene-fix', {method: 'POST'})).status, 200);
  operation.settings.maxFailures = 3;
  serve(json(202, {}), new TypeError('offline'), new TypeError('offline'), new TypeError('offline'));
  const response = await operation.fetch('/api/scene-fix', {method: 'POST'});
  assert.equal(response.status, 504);
  assert.match((await response.json()).error, /再読み込み/);
});

test('the page timeout (AbortController) stops polling', async () => {
  const controller = new AbortController();
  serve(json(202, {}), () => { controller.abort(); return json(200, {status: 'running'}); });
  await assert.rejects(operation.fetch('/api/regenerate/a/1', {method: 'POST', signal: controller.signal}), {name: 'AbortError'});
});
