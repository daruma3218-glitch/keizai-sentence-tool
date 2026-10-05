'use strict';
(() => {
  const content = document.getElementById('queue-content');
  if (!content) return;
  const esc = Studio.escape, status = StudioStatus;
  const freshness = document.getElementById('queue-freshness');
  let previous = JSON.parse(document.getElementById('queue-initial').textContent);
  let timer, inFlight = false, signedOut = false;
  const link = job => '/progress/' + encodeURIComponent(job.id);
  const meta = job => [job.channel, job.creator ? '依頼者（入力名）：' + job.creator : '', job.started ? job.started + ' 受付（日本時間）' : ''].filter(Boolean).map(esc).join(' · ');
  async function fetchStatus(url) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(url, {cache:'no-store', signal:controller.signal});
      return {ok:response.ok, status:response.status, body:response.ok ? await response.json() : null};
    }
    catch (_) { throw new Error('更新できませんでした。最後に確認できた状態を表示しています。'); }
    finally { clearTimeout(timeout); }
  }
  function render(board) {
    document.getElementById('queue-intake').hidden = board.accepting !== false;
    document.getElementById('queue-other').textContent = '素材の手直し・追加：' + (board.other_operations || 0) + ' 件処理中（一括生成とは別）';
    const running = board.running;
    const percent = Math.min(100, Math.max(0, Number(running?.percent) || 0));
    content.innerHTML = (running ? '<a class="queue-running" data-tone="working" href="' + link(running) + '"><div class="queue-job-title">' +
      status.html('running') + '<strong>' + esc(running.title) + '</strong><span class="hint">' + meta(running) + '</span></div>' +
      '<div class="queue-progress"><strong>' + percent + '%</strong><div class="queue-track"><span style="width:' + percent + '%"></span></div><span>詳しい進捗 →</span></div>' +
      (running.message ? '<p class="queue-message">' + esc(running.message) + '</p>' : '') + '</a>' :
      '<p class="queue-empty">' + (board.waiting.length ? '順番に開始する準備をしています。' : '現在、原稿からの一括生成はありません。') + '</p>') +
      (board.waiting.length ? '<div class="queue-list-heading"><strong>順番待ち ' + board.waiting.length + ' 件</strong><span class="hint">次に始まる順</span></div><ol class="queue-waiting">' + board.waiting.map((job, index) =>
        '<li data-tone="queued"><span class="queue-position">' + (index + 1) + '</span><a href="' + link(job) + '"><strong>' + esc(job.title) + '</strong><span>' + meta(job) + '</span></a>' + status.html('queued', index === 0 ? '次に開始' : '順番待ち') + '</li>'
      ).join('') + '</ol>' : '');
  }
  async function finished(job) {
    try {
      const response = await fetchStatus('/api/status/' + encodeURIComponent(job.id));
      if (!response.ok) return;
      const state = response.body;
      if (!['completed','error','failed','interrupted','cancelled'].includes(state.status)) return;
      const element = document.getElementById('queue-finished');
      element.dataset.tone = status.describe(state.status).tone;
      element.innerHTML = status.html(state.status) + '<a href="' + link(job) + '">' + esc(job.title) + '：結果を確認 →</a>';
      element.hidden = false;
    } catch (_) { /* Keep the last confirmed board; never infer successful completion. */ }
  }
  async function poll() {
    clearTimeout(timer);
    if (document.hidden || inFlight || signedOut) return;
    inFlight = true;
    try {
      const response = await fetchStatus('/api/material-generation-board');
      if (response.status === 401) { signedOut = true; throw new Error('ログインが切れました。画面を読み込み直してください。'); }
      if (!response.ok) throw new Error('更新できませんでした。最後に確認できた状態を表示しています。');
      const board = response.body;
      if (!Array.isArray(board.waiting) || !Object.hasOwn(board, 'running')) throw new Error('状況を確認できません。最後の表示を保持しています。');
      render(board);
      if (previous.running && previous.running.id !== board.running?.id && !board.waiting.some(job => job.id === previous.running.id)) await finished(previous.running);
      previous = board;
      freshness.textContent = new Date().toLocaleTimeString('ja-JP', {hour12:false}) + ' 更新 · 5秒ごとに更新';
      freshness.removeAttribute('data-tone');
    } catch (error) {
      freshness.textContent = error.message || '状況を更新できません。最後の表示を保持しています。';
      freshness.dataset.tone = 'attention';
    } finally {
      inFlight = false;
      if (!signedOut && !document.hidden) timer = setTimeout(poll, 5000);
    }
  }
  render(previous);
  document.addEventListener('visibilitychange', () => {
    clearTimeout(timer);
    if (document.hidden) freshness.textContent = '自動更新を一時停止中';
    else poll();
  });
  poll();
})();
