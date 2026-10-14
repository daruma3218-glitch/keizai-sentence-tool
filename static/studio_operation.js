'use strict';
// 時間のかかる操作（場面の直し・その修正・作り直し・素材の別案）を「受け付け → 問い合わせ」で待つ（2026-10-14）。
// 新しい入口（Cloudflare）は応答を約100秒しか待たないため、サーバーはすぐ番号を返し、ここが数秒ごとに結果を問い合わせる。
// 戻り値は fetch の Response と同じように ok・status・json()・text() を使える（呼び出し側の作りはそのまま）。
// 古いサーバー（202 を返さない）や、すぐ返る断り（401・403・429 など）は、その応答をそのまま返す。
(function (root) {
  const settings = {
    pollMs: 3000,
    maxWaitMs: 30 * 60 * 1000,
    maxFailures: 20,    // 受け付けた後、問い合わせが続けて届かない回数の上限（間隔を延ばすので、およそ8分）
    verifyFailures: 3,  // 受け付けたか分からない時（返事が届かない・5xx）に、同じ番号で確かめる回数
    maxBackoffMs: 30000,
  };
  const LOGIN_AGAIN = 'ログインが切れました。ページを再読み込みして、もう一度ログインしてください。';

  function newId() {
    const bytes = new Uint8Array(16);
    root.crypto.getRandomValues(bytes);
    return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
  }

  function aborted() {
    try { return new DOMException('The operation was aborted.', 'AbortError'); }
    catch (_) { const error = new Error('The operation was aborted.'); error.name = 'AbortError'; return error; }
  }

  function sleep(ms, signal) {
    return new Promise((resolve, reject) => {
      if (signal && signal.aborted) { reject(aborted()); return; }
      const timer = setTimeout(resolve, ms);
      if (signal) signal.addEventListener('abort', () => { clearTimeout(timer); reject(aborted()); }, {once: true});
    });
  }

  function asResponse(status, body, text) {
    const raw = text !== undefined ? String(text) : JSON.stringify(body === undefined ? {} : body);
    return {
      ok: status >= 200 && status < 300,
      status,
      fromOperation: true,
      json: async () => (body !== undefined ? body : JSON.parse(raw)),
      text: async () => raw,
    };
  }

  // 新しい入口のログインが切れると、Cloudflare のログインの画面へ転送される。追わずに、すぐ知らせる
  const loginExpired = response => response && response.type === 'opaqueredirect';

  // unknown: 受け付けたか分からない時の確かめ。受け付けていなければ（404）notAccepted を返し、届かなければ早めに諦める
  async function poll(id, options, unknown) {
    const signal = options.signal;
    const started = Date.now();
    let failures = 0, confirmed = !unknown;
    const limit = () => (confirmed ? settings.maxFailures : settings.verifyFailures);
    while (Date.now() - started < settings.maxWaitMs) {
      await sleep(failures ? Math.min(settings.pollMs * 2 ** failures, settings.maxBackoffMs) : settings.pollMs, signal);
      let response;
      try {
        response = await root.fetch('/api/operations/' + encodeURIComponent(id),
          {credentials: 'same-origin', cache: 'no-store', redirect: 'manual', signal});
      } catch (error) {
        if (error && error.name === 'AbortError') throw error;
        failures += 1;  // 回線の切れ目。間隔を延ばして続ける
        if (failures >= limit()) break;
        if (options.onWait) options.onWait({failures});
        continue;
      }
      if (loginExpired(response)) return asResponse(401, {ok: false, error: LOGIN_AGAIN});
      if (response.status === 404 && unknown) return unknown.notAccepted;
      if (response.status === 401 || response.status === 404) return response;
      const state = response.ok ? await response.json().catch(() => null) : null;
      if (!state) {
        failures += 1;
        if (failures >= limit()) break;
        continue;
      }
      failures = 0;
      confirmed = true;
      if (state.status === 'done' && state.result) return asResponse(state.result.status, state.result.body, state.result.text);
      if (state.status === 'interrupted' || state.status === 'unknown') return asResponse(409, {ok: false, error: state.error});
      if (options.onWait) options.onWait({elapsed: state.elapsed, waiting: !!state.waiting});
    }
    if (!confirmed && unknown) return unknown.notAccepted;
    return asResponse(504, {ok: false, error: failures
      ? '結果を問い合わせられないままです。ページを再読み込みしてください（ログインが切れた時は、もう一度ログインしてください）。サーバーで終わった結果は、再読み込みの後に確かめられます。'
      : '30分待っても終わりませんでした。ページを再読み込みして結果を確かめてください。'});
  }

  async function operationFetch(url, options = {}) {
    const id = newId();
    const {onWait, ...init} = options;
    const headers = {...(init.headers || {}), 'X-Studio-Async': '1', 'X-Studio-Operation': id};
    const pollOptions = {signal: init.signal, onWait};
    let response;
    try {
      response = await root.fetch(url, {...init, headers, credentials: 'same-origin', redirect: 'manual'});
    } catch (error) {
      if (error && error.name === 'AbortError') throw error;
      // 受け付けの返事だけが届かなかったかもしれない。同じ番号で確かめる（同じ番号は二度動かない）
      return poll(id, pollOptions, {notAccepted: asResponse(503, {ok: false,
        error: '送れませんでした。通信を確かめて、もう一度押してください。'})});
    }
    if (loginExpired(response)) return asResponse(401, {ok: false, error: LOGIN_AGAIN});
    if (response.status >= 500) {
      // 途中の中継（Cloudflare・Render）の 5xx は、受け付け済みかもしれない。押し直しで二重に作らないよう、同じ番号で確かめる
      return poll(id, pollOptions, {notAccepted: response});
    }
    if (response.status !== 202) return response;
    return poll(id, pollOptions);
  }

  root.StudioOperation = {fetch: operationFetch, settings};
  if (typeof module !== 'undefined' && module.exports) module.exports = root.StudioOperation;
})(typeof window !== 'undefined' ? window : globalThis);
