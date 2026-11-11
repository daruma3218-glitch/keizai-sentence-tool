'use strict';
(() => {
  const $ = id => document.getElementById(id), esc = Studio.escape;
  const poseNames = {pointing:'指差し', thinking:'考える', surprised:'驚く', explaining:'両手で説明', good:'グッド'};
  const base = '/api/material-jobs/' + encodeURIComponent(window.materialJobId);
  let data = null, filter = 'all', busy = false, operation = null, flagNo = '', initialView = true, canSavePosition = false;
  const storageKey = 'material.selection.v1.' + window.materialJobId;
  const pendingKey = 'material.pending.v1.' + window.materialJobId;
  const pending = new Map(), watching = new Set();
  try {
    const saved = JSON.parse(localStorage.getItem(pendingKey) || '[]');
    for (const op of saved) {
      if (/^[a-zA-Z0-9_-]{16,64}$/.test(op.request_id) && /^\d+$/.test(String(op.no))) {
        pending.set(op.request_id, {...op, state:'checking'});
      }
    }
  } catch (_) { /* Progress also lives on the server. */ }
  function savePending() {
    try { localStorage.setItem(pendingKey, JSON.stringify([...pending.values()].filter(op => op.state !== 'failed'))); }
    catch (_) { /* Optional storage; generation continues on the server. */ }
  }
  function sceneOperation(no) {
    const local = [...pending.values()].filter(op => String(op.no) === String(no));
    return local.find(op => op.state !== 'failed') ||
      Object.entries(data?.library.requests || {}).map(([request_id, op]) => ({...op, request_id, state:'running'}))
        .find(op => String(op.no) === String(no) && op.status === 'running') || local[local.length - 1];
  }
  function updateOperations() {
    const active = new Set();
    for (const op of Object.values(data?.library.requests || {})) if (op.status === 'running') active.add(String(op.no));
    for (const op of pending.values()) if (op.state !== 'failed') active.add(String(op.no));
    const labels = {submitting:'候補の作成を受け付けています…', waiting:'候補の作成は順番待ちです',
      running:'候補を作成中です', checking:'候補の処理結果を確認しています…', uncertain:'通信を確認してください。処理結果は「素材を読み込む」で確認できます'};
    document.querySelectorAll('.scene').forEach(scene => {
      const op = sceneOperation(scene.dataset.no), working = !!op && op.state !== 'failed';
      const status = scene.querySelector('.scene-operation');
      if (status) {
        status.hidden = !op;
        status.dataset.tone = op?.state === 'failed' || op?.state === 'uncertain' ? 'attention' : 'working';
        status.textContent = op ? (op.state === 'failed' ? op.error : labels[op.state] || labels.running) : '';
      }
      scene.querySelectorAll('[data-action="generate"], [data-action="edit"], [data-action="flip"], [data-action="upload"]').forEach(button => {
        button.disabled = busy || working;
        if (button.dataset.action === 'generate') button.textContent = working ? (op.state === 'waiting' ? '順番待ち…' : '候補を作成中…') : '候補を生成';
      });
    });
    $('operation-state').hidden = !active.size;
    $('operation-state').dataset.tone = 'working';
    $('operation-state').textContent = active.size + ' 場面の候補を作成・確認中です。ほかの場面の確認・採用・候補追加を続けられます。';
  }
  function applyPayload(next, background = false) {
    if (data && next.library.revision < data.library.revision) return;
    if (background && data && next.library.revision === data.library.revision) { updateOperations(); return; }
    const anchor = background && [...document.querySelectorAll('.scene')].find(el => el.getBoundingClientRect().bottom > 0);
    const position = anchor ? {no:anchor.dataset.no, offset:anchor.getBoundingClientRect().top, y:window.scrollY} : null;
    const focused = background && document.activeElement;
    const focusScene = focused?.closest?.('.scene')?.dataset.no;
    const focusCandidate = focused?.closest?.('.candidate')?.dataset.candidate;
    const focusAction = focused?.dataset?.action;
    data = next; draw();
    if (focusScene && focusAction) {
      const scene = [...document.querySelectorAll('.scene')].find(el => el.dataset.no === focusScene);
      const scope = focusCandidate ? [...(scene?.querySelectorAll('.candidate') || [])].find(el => el.dataset.candidate === focusCandidate) : scene;
      scope?.querySelector('[data-action="' + focusAction + '"]')?.focus({preventScroll:true});
    }
    if (position) restorePosition(position);
  }
  async function runPending(op, body) {
    if (watching.has(op.request_id)) return;
    watching.add(op.request_id);
    const onWait = state => {
      op.state = state.failures ? 'checking' : state.waiting ? 'waiting' : 'running';
      updateOperations();
    };
    try {
      if (body) {
        await Studio.api(base + '/variant', {method:'POST', body, poll:true, operationId:op.request_id, onWait});
      } else {
        const response = await StudioOperation.resume(op.operation_id || op.request_id, {onWait});
        const result = await response.json().catch(() => ({}));
        if (!response.ok) { const error = new Error(result.error || '処理結果を確認できませんでした。素材を読み込んで確認してください'); error.status = response.status; throw error; }
      }
      pending.delete(op.request_id);
      Studio.notice('場面 ' + op.no + ' に候補を追加しました。比べてから採用できます。');
    } catch (error) {
      op.state = !error.status || error.status >= 500 || error.status === 401 ? 'uncertain' : 'failed';
      op.error = error.message;
      Studio.notice('場面 ' + op.no + '：' + error.message, true);
    } finally {
      watching.delete(op.request_id); savePending(); updateOperations();
      // The stored operation response may predate a later adoption. Read the
      // current library and never replace newer UI state with an older revision.
      try {
        const latest = await Studio.api(base, {cache:'no-store'});
        const saved = latest.library.requests[op.request_id];
        if (saved?.status === 'completed') pending.delete(op.request_id);
        else if (saved?.status === 'failed' && pending.has(op.request_id)) {
          op.state = 'failed'; op.error ||= '候補を作成できませんでした。元画像と採用状態は保持しています。';
        }
        savePending(); applyPayload(latest, true);
      }
      catch (_) { Studio.notice('候補の表示を更新できませんでした。「素材を読み込む」で確認してください。', true); }
    }
  }
  function resumePending() {
    for (const [request_id, op] of Object.entries(data?.library.requests || {})) {
      if (op.status === 'running' && op.operation_id && !pending.has(request_id)) pending.set(request_id, {...op, request_id, state:'checking'});
    }
    for (const op of pending.values()) if (op.state !== 'failed') runPending(op);
    updateOperations();
  }
  let lastVisit = null, reviewNo = '', reviewCandidate = '', reviewReference = '', reviewTrigger = null, firstCommentRow = null;
  let commentScenes = [];
  try { lastVisit = MaterialView.saved(JSON.parse(localStorage.getItem(storageKey))); } catch (_) { /* Storage is optional. */ }
  if (lastVisit) { filter = lastVisit.filter; $('search').value = lastVisit.query; $('restore-view').hidden = false; }
  const currentView = () => ({filter, chapter:$('chapter-filter').value, query:$('search').value.trim(), openScenes:comments.openScenes});
  function savePosition() {
    if (!canSavePosition || !data) return;
    const scene = [...document.querySelectorAll('.scene')].find(el => el.getBoundingClientRect().bottom > 0);
    try { localStorage.setItem(storageKey, JSON.stringify({...currentView(), no:scene?.dataset.no || '', offset:scene?.getBoundingClientRect().top || 0, y:window.scrollY})); } catch (_) { /* Selection remains usable. */ }
  }
  function restorePosition(view) {
    if (!view) { canSavePosition = true; return; }
    canSavePosition = false;
    requestAnimationFrame(() => {
      const scenes = [...document.querySelectorAll('.scene')];
      const scene = scenes.find(el => el.dataset.no === view.no) || scenes.find(el => Number(el.dataset.no) >= Number(view.no));
      window.scrollTo({top:scene ? window.scrollY + scene.getBoundingClientRect().top - view.offset : view.y, behavior:'instant'});
      requestAnimationFrame(() => { canSavePosition = true; });
    });
  }
  const src = candidate => '/materials/asset/' + encodeURIComponent(window.materialJobId) + '/' + encodeURIComponent(candidate.id);
  const sceneCandidates = no => data ? MaterialView.candidates(data.library, no, $('show-hidden-candidates').checked) : [];
  const display = row => MaterialView.mode(row, data.library);
  const comments = MaterialComments.mount({
    base, sceneCandidates, assetUrl:src,
    switchImage:id => { reviewCandidate = id; renderReview(); $('comment-history-bar').focus(); },
    onChange() {
      $('comment-summary').textContent = comments.summary;
      if (!data) return;
      if (filter === 'comments') { draw(); return; }
      document.querySelector('[data-filter="comments"]').textContent = '未対応コメント ' + comments.openScenes.size;
      document.querySelectorAll('.scene').forEach(scene => {
        const count = comments.openScenes.get(scene.dataset.no);
        scene.querySelector('.scene-comments').innerHTML = count ? StudioStatus.html('attention', '未対応コメント ' + count) : '';
        scene.querySelectorAll('[data-action="comments"]').forEach(button => {
          button.textContent = comments.label(button.closest('.candidate').dataset.candidate);
        });
      });
    }
  });
  function setBusy(value) {
    busy = value;
    $('comment-review-start').disabled = value || !firstCommentRow;
    document.querySelectorAll('#rows button, #refresh, #undo, #variant-submit, #review-adopt, #review-adopt-next, #review-skip, #review-next, #review-use-reference, #review-candidate-id, #review-reference-id, #review-visibility, #show-hidden-candidates, #review-show-hidden').forEach(button => { button.disabled = value || button.dataset.visibilityLocked === 'true'; });
    updateReviewNavigation();
    updateOperations();
  }
  function draw() {
    if (!data) return;
    const lib = data.library, rows = data.rows;
    const hiddenCount = Object.values(lib.hidden_candidates || {}).filter(Boolean).length;
    $('hidden-candidates-control').hidden = !hiddenCount;
    $('hidden-candidates-count').textContent = '（' + hiddenCount + '枚）';
    if (!hiddenCount) $('show-hidden-candidates').checked = false;
    const required = rows.filter(row => display(row) === 'image');
    const adopted = required.filter(row => lib.selections[String(row.no)]).length;
    $('adoption-count').textContent = adopted + ' / ' + required.length + ' 場面の素材を採用';
    const adoptionProgress = $('adoption-progress');
    if (adoptionProgress) { adoptionProgress.max = Math.max(required.length, 1); adoptionProgress.value = adopted; }
    StudioStatus.apply($('job-status'), data.status);
    const remaining = MaterialView.remaining(rows, lib).length;
    const pending = required.length - adopted;
    const flags = rows.filter(row => lib.flags[String(row.no)]).length;
    $('next-action').textContent = (pending ? 'あと ' + pending + ' 場面の素材を採用' : '必要な素材は採用済み') + (flags ? ' · 要修正 ' + flags + ' 場面' : '') + (remaining ? ' · 確認が必要 ' + remaining + ' 場面' : ' · 書き出し時に最新版を照合します');
    const choosing = remaining > 0 || !rows.length;
    ['generation','selection','export'].forEach((step, index) => {
      const element = $('library-step-' + step);
      const current = data.status !== 'completed' ? 0 : choosing ? 1 : 2;
      element.setAttribute('aria-current', index === current ? 'step' : 'false');
      element.classList.toggle('is-done', index < current);
    });
    $('download').classList.toggle('dark', !choosing && data.status === 'completed');
    $('review-next').classList.toggle('dark', pending > 0);
    $('review-next').textContent = pending ? '次の未採用を確認 →' : remaining ? '確認が必要な場面へ →' : '採用した素材を見直す';
    $('review-next').classList.toggle('dark', choosing);
    $('next-action').textContent = pending ? '画像を押して比較 → 採用して次へ。あと ' + pending + ' 場面。' : remaining ? '要修正や表示方法を確認してください。採用済みでも確認が残っている場面があります。' : data.status === 'completed' ? '選び終わりました。「採用素材を書き出す」で編集担当へ渡すZIPを保存します。' : '完成した素材から確認できます。生成の完了までお待ちください。';
    document.querySelectorAll('[data-filter]').forEach(button => {
      const labels = {all:'すべて',pending:'未採用',selected:'採用済み',flagged:'要修正',comments:'未対応コメント'};
      const count = {all:rows.length,pending,selected:adopted,flagged:flags,comments:comments.openScenes.size}[button.dataset.filter];
      button.textContent = labels[button.dataset.filter] + ' ' + count;
    });
    const exported = data.export?.last;
    $('export-state').textContent = exported ? (data.export.changed ? 'この画面での前回書き出し後に変更があります。編集側へ渡すときは、もう一度書き出してください。' : 'この画面で前回書き出した採用内容と一致しています。') : '';
    $('export-state').classList.toggle('status-text', !!data.export?.changed);
    $('export-state').dataset.tone = data.export?.changed ? 'attention' : 'neutral';
    $('selection-summary').innerHTML = StudioStatus.html('selection_pending', '未採用 ' + (required.length - adopted) + ' 場面') +
      StudioStatus.html('adopted', '採用済み ' + adopted + ' 場面') + StudioStatus.html('flagged', '要修正 ' + rows.filter(row => lib.flags[String(row.no)]).length + ' 場面');
    const selectedChapter = initialView ? lastVisit?.chapter || '' : $('chapter-filter').value;
    const chapters = new Map(rows.map(row => [MaterialView.chapter(row), row.chapter_title || (row.chapter_index != null ? '第' + row.chapter_index + '章' : '章の情報なし')]));
    $('chapter-filter').innerHTML = '<option value="">すべての章</option>' + [...chapters].map(([id, label]) => '<option value="' + esc(id) + '">' + esc(label) + '</option>').join('');
    $('chapter-filter').value = chapters.has(selectedChapter) ? selectedChapter : '';
    document.querySelectorAll('[data-filter]').forEach(button => { const active = button.dataset.filter === filter; button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active)); });
    const visible = rows.filter(row => MaterialView.matches(row, lib, currentView()));
    firstCommentRow = visible.find(row => sceneCandidates(row.no).length);
    if (pending && !MaterialView.nextPending(rows, lib, '', currentView())) $('review-next').textContent = 'この範囲の素材を見直す';
    $('visible-count').textContent = visible.length + ' / ' + rows.length + ' 場面を表示';
    $('rows').innerHTML = visible.map(row => {
      const no = String(row.no), candidates = sceneCandidates(no), selected = lib.selections[no], mode = display(row);
      const hiddenInScene = (lib.candidates[no] || []).filter(c => lib.hidden_candidates?.[c.id]).length;
      const selectionState = MaterialView.state(row, lib);
      return '<article tabindex="-1" class="scene" data-tone="' + StudioStatus.describe(selectionState).tone + '" data-selection-tone data-no="' + esc(no) + '"><div class="scene-caption"><div><span class="scene-number">' +
        esc(String(row.no).padStart(3,'0')) + '</span><span class="pill">' + esc(row.chapter_title || ('第' + (row.chapter_index ?? 1) + '章')) +
        '</span><span class="selection-state">' + StudioStatus.html(selectionState) + '</span><span class="scene-comments">' +
        (comments.openScenes.has(no) ? StudioStatus.html('attention', '未対応コメント ' + comments.openScenes.get(no)) : '') + '</span>' +
        '</div><p class="sentence">' + esc(row.sentence) + '</p><div class="display-options">' +
        '<span class="hint">表示：</span><button data-action="image" class="' + (mode === 'image' ? 'chosen' : '') + '">素材を切り替え</button><button data-action="hold" class="' + (mode === 'hold' ? 'chosen' : '') + '">前の素材を継続</button>' +
        '<button data-action="none" class="' + (mode === 'none' ? 'chosen' : '') + '">素材なし</button>' +
        '<button class="compare-scene" data-action="compare">大きく比較・採用</button><button data-action="flag">' + (lib.flags[no] ? '修正メモを変更' : '要修正にする') + '</button></div>' +
        (lib.flags[no] ? '<p class="hint flag-label">' + esc(lib.flags[no]) + '</p>' : '') +
        '<p class="scene-operation status-text" role="status" aria-live="polite" hidden></p>' +
        (hiddenInScene ? '<p class="hint">非表示の候補 ' + hiddenInScene + '枚' + (!candidates.length ? ' · 上の「非表示の候補も表示」から戻せます。' : '') + '</p>' : '') +
        '</div><div class="candidate-strip">' + candidates.map(c => {
          const chosen = c.id === selected, hidden = !!lib.hidden_candidates?.[c.id];
          return '<div class="candidate ' + (chosen ? 'selected' : hidden ? 'candidate-hidden' : '') + '" data-candidate="' + esc(c.id) + '">' +
            '<button class="image-button" data-action="preview" aria-label="候補を拡大"><img loading="lazy" src="' + src(c) + '" alt="' + esc(c.label) + '"></button>' +
            '<div class="candidate-body"><div class="candidate-caption"><strong>' + esc(c.label) + '</strong><span class="hint">' + esc(c.created_at?.slice(0,10)) + '</span></div>' +
            (hidden ? '<p class="hidden-candidate-label">非表示</p>' : '') +
            (poseNames[c.presenter?.pose_id] ? '<p class="hint">すあし社長：' + esc(poseNames[c.presenter.pose_id]) + '</p>' : '') +
            '<button type="button" class="button wide candidate-comments" data-action="comments">' + esc(comments.label(c.id)) + '</button>' +
            (hidden ? '<button class="button wide" data-action="restore">候補に戻す</button>' :
              '<button class="button ' + (chosen ? 'adopted' : '') + ' wide" data-action="' + (chosen ? 'clear' : 'select') + '">' + (chosen ? '✓ 採用中（解除する）' : 'この候補を採用') + '</button>' +
              '<div class="candidate-tools"><button data-action="edit">この画像を直す</button><details class="candidate-more"><summary>その他</summary><button data-action="flip">左右反転</button><button data-action="hide" data-visibility-locked="' + chosen + '"' + (chosen ? ' disabled title="採用を解除してから非表示にできます"' : '') + '>候補から非表示</button></details></div>') +
            (c.web_source_url ? '<a class="hint" href="' + esc(/^https?:\/\//.test(c.web_source_url) ? c.web_source_url : '#') + '" target="_blank" rel="noopener">出典を確認 ↗</a>' : '') +
            '</div></div>';
        }).join('') + '<div class="add-candidate"><span>＋</span><strong>' + (candidates.length ? '別の案も比べる' : 'この場面の素材を追加') + '</strong>' +
        '<button class="button" data-action="generate">候補を生成</button><button class="text-button" data-action="upload">画像を取り込む</button></div></div></article>';
    }).join('') || '<div class="empty"><p>' + (rows.length ? '該当する場面がありません。絞り込みを解除して確認できます。' : '原稿を解析中、またはまだ素材がありません。「詳しい進捗・手直し」から確認できます。') + '</p></div>';
    const operations = Object.values(lib.requests);
    const requested = operations.reduce((n, op) => n + (op.requested_images || 0), 0);
    $('cost-note').textContent = 'この画面からの追加生成：' + requested + ' 案を依頼。金額は未集計です。利用先の請求と照合して振り返ります。';
    if ($('preview-dialog').open) renderReview();
    setBusy(busy);
    if (initialView) { initialView = false; restorePosition(lastVisit); }
  }
  async function refresh() {
    if (busy) return;
    setBusy(true);
    $('operation-state').hidden = false;
    $('operation-state').dataset.tone = 'working';
    $('operation-state').textContent = '生成済みの画像を候補に取り込んでいます。画像の再生成はしません。';
    try { applyPayload(await Studio.api(base + '/sync', {method:'POST'})); resumePending(); }
    catch (error) { Studio.notice(error.message, true); $('operation-state').dataset.tone = 'attention'; $('operation-state').textContent = error.message; }
    finally { setBusy(false); }
  }
  async function select(no, action, candidate, note) {
    if (busy) return;
    setBusy(true);
    try {
      applyPayload(await Studio.api(base + '/selection', {method:'POST', body:JSON.stringify({revision:data.library.revision,
        selection_revision:data.library.selection_revision, no, action, candidate_id:candidate, note})}));
      Studio.notice(action === 'hide' ? '候補から非表示にしました。画像とコメントは残っています。' : action === 'restore' ? '候補に戻しました。' : action === 'undo' ? '直前の操作を戻しました。' : '保存しました。');
      return true;
    } catch (error) { Studio.notice(error.message, true); if ($('preview-dialog').open) $('review-status').textContent = error.message; return false; }
    finally { setBusy(false); }
  }
  function variant(no, kind, candidate) {
    const current = sceneOperation(no);
    if (current && current.state !== 'failed') return;
    const row = data.rows.find(row => String(row.no) === no);
    operation = {no, kind, candidate_id:candidate?.id || '', request_id:crypto.randomUUID().replaceAll('-',''), revision:data.library.revision};
    const labels = {generate:'別の候補を作る', edit:'この候補を手直し', flip:'左右を反転した候補を追加', upload:'画像を候補に取り込む'};
    $('variant-title').textContent = labels[kind];
    $('variant-scene').textContent = row.sentence;
    $('instruction').value = ''; $('image-file').value = ''; $('variant-count').value = '1';
    const ai = kind === 'generate' || kind === 'edit';
    comments.variant(ai ? no : '');
    if ($('pose-field')) { $('pose-field').hidden = !ai; $('suasi-pose').value = ''; }
    $('instruction').hidden = $('instruction-label').hidden = !ai;
    $('instruction').required = kind === 'edit';
    $('image-file').hidden = $('file-label').hidden = kind !== 'upload';
    $('image-file').required = kind === 'upload';
    $('count-field').hidden = !ai;
    $('variant-preview').hidden = !candidate;
    if (candidate) $('variant-preview').src = src(candidate);
    $('variant-submit').textContent = ai ? '候補を生成する' : kind === 'flip' ? '反転して追加' : '取り込む';
    $('variant-help').textContent = kind === 'flip' ? '画像内の文字も反転します。文字がある素材は、指示して手直ししてください。元の候補は残ります。' :
      ai ? '画像生成を実行します（利用先の従量料金が発生します）。元の候補・採用状態は残ります。' : '元の候補を残して取り込みます。PNG・JPEG・WebPに対応しています。';
    $('variant-dialog').showModal();
  }
  $('rows').addEventListener('click', event => {
    const button = event.target.closest('button[data-action]'); if (!button || busy) return;
    const no = button.closest('.scene').dataset.no, action = button.dataset.action;
    const id = button.closest('.candidate')?.dataset.candidate;
    const candidate = (data.library.candidates[no] || []).find(c => c.id === id);
    if (action === 'preview' || action === 'compare' || action === 'comments') {
      openReview(no, candidate?.id, action === 'comments' ? 'comments' : 'compare');
    } else if (['generate','edit','flip','upload'].includes(action)) variant(no, action, candidate);
    else if (action === 'flag') {
      flagNo = no;
      $('flag-note').value = data.library.flags[no] || '';
      $('clear-flag').hidden = !data.library.flags[no];
      $('flag-dialog').showModal();
    } else select(no, action, id);
  });
  $('show-hidden-candidates').addEventListener('change', () => draw());

  function reviewScenes() {
    if ($('preview-dialog').dataset.mode === 'comments') return commentScenes;
    // 状態の絞り込みは採用直後に変わるため、閲覧中は章・検索の範囲を順にたどる。
    return data ? data.rows.filter(row => MaterialView.matches(row, data.library, {...currentView(), filter:'all'})) : [];
  }
  $('comment-review-start').addEventListener('click', () => {
    if (!firstCommentRow || busy) return;
    openReview(firstCommentRow.no, '', 'comments');
  });
  function updateReviewNavigation() {
    const rows = reviewScenes(), index = rows.findIndex(row => String(row.no) === reviewNo);
    $('comment-prev-scene').disabled = $('review-prev-scene').disabled = busy || index <= 0;
    $('comment-next-scene').disabled = $('review-next-scene').disabled = busy || index < 0 || index >= rows.length - 1;
    $('comment-position').textContent = $('review-position').textContent = index < 0 ? '' : (index + 1) + ' / ' + rows.length + ' 場面';
  }
  function moveReview(direction) {
    if (busy) return;
    const rows = reviewScenes(), index = rows.findIndex(row => String(row.no) === reviewNo);
    const next = index >= 0 && rows[index + direction];
    if (next) openReview(next.no);
  }
  $('review-prev-scene').addEventListener('click', () => moveReview(-1));
  $('review-next-scene').addEventListener('click', () => moveReview(1));
  $('comment-prev-scene').addEventListener('click', () => moveReview(-1));
  $('comment-next-scene').addEventListener('click', () => moveReview(1));
  function showCommentScript(show) {
    $('comment-script-toggle').setAttribute('aria-expanded', String(show));
    $('comment-script').hidden = !show;
  }
  $('comment-script-toggle').addEventListener('click', () => showCommentScript($('comment-script').hidden));
  function renderReview() {
    const row = data.rows.find(row => String(row.no) === reviewNo);
    if (!row) return;
    const candidates = sceneCandidates(reviewNo), chosen = data.library.selections[reviewNo];
    if (!candidates.some(c => c.id === reviewCandidate)) reviewCandidate = candidates.find(c => c.id === chosen)?.id || candidates[0]?.id || '';
    if (!candidates.some(c => c.id === reviewReference) || reviewReference === reviewCandidate) reviewReference = candidates.find(c => c.id !== reviewCandidate)?.id || '';
    $('review-location').textContent = '場面 ' + String(row.no).padStart(3,'0') + ' · ' + (row.chapter_title || '章 ' + (row.chapter_index ?? 1));
    const reading = $('preview-dialog').dataset.mode === 'comments';
    $('review-sentence').hidden = reading;
    $('review-sentence').textContent = row.sentence;
    $('comment-script').textContent = reading ? row.sentence : '';
    $('comment-script-toggle').hidden = !reading || !row.sentence;
    if ($('comment-script-toggle').hidden) showCommentScript(false);
    $('review-candidate-label').classList.toggle('sr-only', reading);
    $('review-candidate-id').hidden = reading && candidates.length < 2;
    $('review-flag').hidden = !data.library.flags[reviewNo];
    $('review-flag').textContent = '要修正：' + (data.library.flags[reviewNo] || '') + '（採用後もメモは残ります）';
    const options = candidates.map((c,i) => '<option value="' + esc(c.id) + '">候補 ' + (i + 1) + (reading && c.label === '最初の候補' ? '' : ' · ' + esc(c.label)) + (c.id === chosen ? ' · 採用中' : '') + (data.library.hidden_candidates?.[c.id] ? ' · 非表示' : '') + '</option>').join('');
    $('review-candidate-id').innerHTML = $('review-reference-id').innerHTML = options;
    $('review-candidate-id').value = reviewCandidate; $('review-reference-id').value = reviewReference;
    const focused = candidates.find(c => c.id === reviewCandidate), reference = candidates.find(c => c.id === reviewReference);
    const isHidden = !!data.library.hidden_candidates?.[focused?.id];
    if (focused) $('preview-image').src = src(focused); else $('preview-image').removeAttribute('src');
    const showReference = !!reference && $('preview-dialog').dataset.mode !== 'comments';
    if (showReference) $('review-reference-image').src = src(reference); else $('review-reference-image').removeAttribute('src');
    $('review-reference').hidden = !showReference;
    $('review-grid').classList.toggle('single', !reference);
    $('review-grid').hidden = !focused; $('review-no-candidate').hidden = !!focused;
    $('review-adopt').hidden = $('review-adopt-next').hidden = !focused || isHidden;
    $('review-visibility').hidden = !focused;
    $('review-visibility').textContent = isHidden ? '候補に戻す' : '候補から非表示';
    $('review-visibility').dataset.visibilityLocked = String(!focused || focused.id === chosen);
    $('review-visibility').disabled = busy || !focused || focused.id === chosen;
    $('review-visibility').title = focused?.id === chosen ? '採用を解除してから非表示にできます' : '';
    $('review-show-hidden').hidden = !!focused || !MaterialView.candidates(data.library, reviewNo, true).length;
    $('review-chosen').textContent = isHidden ? '非表示の候補 · 候補に戻すと採用できます' : focused?.id === chosen ? '✓ 採用中の画像' :
      $('preview-dialog').dataset.mode === 'comments' ? '未採用の候補' : '採用すると、この場面で使う画像として保存されます。';
    comments.selectImage(reviewNo, reviewCandidate);
    const next = MaterialView.nextPending(data.rows, data.library, reviewNo, currentView());
    $('review-skip').hidden = !next;
    $('review-more').hidden = !focused && !next;
    $('review-adopt-next').textContent = next ? '採用して次へ →' : '採用して一覧へ戻る';
    $('review-adopt').textContent = focused?.id === chosen ? '採用中の候補を保存' : '採用してこの場面にとどまる';
    updateReviewNavigation();
  }
  function openReview(no, candidate, mode = $('preview-dialog').open ? $('preview-dialog').dataset.mode : 'compare') {
    const dialog = $('preview-dialog'), opening = !dialog.open;
    if (opening) {
      reviewTrigger = document.activeElement;
      // Keep the route stable even when the reviewer resolves the last open thread on the current scene.
      if (mode === 'comments') commentScenes = data.rows.filter(row =>
        sceneCandidates(row.no).length && MaterialView.matches(row, data.library, currentView()));
    }
    dialog.dataset.mode = mode;
    if (opening) showCommentScript(false);
    $('review-title').textContent = mode === 'comments' ? '画像のコメント' : '候補を比較・採用';
    $('review-candidate-label').textContent = mode === 'comments' ? '確認する画像' : '採用する候補';
    reviewNo = String(no); reviewCandidate = candidate || ''; reviewReference = '';
    if (mode === 'comments' && !reviewCandidate) {
      const candidates = sceneCandidates(reviewNo);
      const unresolved = filter === 'comments' && candidates.find(c => comments.hasOpen(c.id));
      reviewCandidate = unresolved?.id || data.library.selections[reviewNo] || candidates.at(-1)?.id || '';
    }
    $('review-status').textContent = '';
    $('review-more').open = false;
    renderReview();
    if (opening) dialog.showModal();
    dialog.scrollTop = $('review-body').scrollTop = 0;
    if (mode === 'compare' || opening) $(mode === 'comments' ? 'review-title' : 'review-candidate-id').focus({preventScroll:true});
  }
  function reviewNext() {
    if (!data || busy) return;
    const first = MaterialView.nextPending(data.rows, data.library, '', currentView()) || MaterialView.remaining(data.rows, data.library).find(row => MaterialView.matches(row, data.library, {...currentView(), filter:'all'})) || data.rows.find(row => MaterialView.matches(row, data.library, {...currentView(), filter:'all'}));
    if (first) openReview(first.no); else Studio.notice('この条件に合う場面はありません。絞り込みを解除してください。');
  }
  $('review-next').addEventListener('click', reviewNext);
  $('review-candidate-id').addEventListener('change', event => { reviewCandidate = event.target.value; renderReview(); });
  $('review-use-reference').addEventListener('click', () => {
    if (busy || !reviewReference) return;
    [reviewCandidate, reviewReference] = [reviewReference, reviewCandidate];
    renderReview();
    $('review-status').textContent = '採用する候補を切り替えました。下のボタンで確定します。';
    $('review-adopt-next').focus({preventScroll:true});
  });
  $('review-reference-id').addEventListener('change', event => { reviewReference = event.target.value; renderReview(); });
  $('review-skip').addEventListener('click', () => {
    if (busy) return;
    const next = MaterialView.nextPending(data.rows, data.library, reviewNo, currentView());
    if (next) openReview(next.no);
  });
  async function adoptReview(advance) {
    if (busy || !reviewCandidate) return;
    $('review-status').textContent = '採用を保存しています…';
    if (!await select(reviewNo, 'select', reviewCandidate)) return;
    if (!$('preview-dialog').open) return;
    $('review-status').textContent = '採用を保存しました。';
    $('review-more').open = false;
    if (advance) {
      const next = MaterialView.nextPending(data.rows, data.library, reviewNo, currentView());
      if (next) openReview(next.no);
      else { $('preview-dialog').close(); Studio.notice('この範囲の未採用素材を確認しました。要修正メモと書き出し前の状態も確認できます。'); }
    }
  }
  $('review-adopt').addEventListener('click', () => adoptReview(false));
  $('review-adopt-next').addEventListener('click', () => adoptReview(true));
  $('review-visibility').addEventListener('click', async () => {
    if (busy || !reviewCandidate) return;
    const action = data.library.hidden_candidates?.[reviewCandidate] ? 'restore' : 'hide';
    if (await select(reviewNo, action, reviewCandidate)) {
      $('review-status').textContent = action === 'hide' ? '候補から非表示にしました。画像とコメントは残っています。' : '候補に戻しました。';
      ($('review-visibility').hidden ? $('review-show-hidden') : $('review-visibility')).focus({preventScroll:true});
    }
  });
  $('review-show-hidden').addEventListener('click', () => { $('show-hidden-candidates').checked = true; draw(); });
  $('review-add').addEventListener('click', () => {
    $('preview-dialog').close();
    const scene = [...document.querySelectorAll('.scene')].find(el => el.dataset.no === reviewNo);
    scene?.scrollIntoView({block:'center',behavior:'instant'});
    scene?.querySelector('[data-action="generate"]')?.focus({preventScroll:true});
  });
  $('preview-dialog').addEventListener('close', () => {
    const scene = [...document.querySelectorAll('.scene')].find(el => el.dataset.no === reviewNo);
    const trigger = $('preview-dialog').dataset.mode === 'comments' && reviewTrigger?.isConnected && reviewTrigger;
    (trigger || scene || $('review-next')).focus({preventScroll:true});
  });
  $('variant-form').addEventListener('submit', async event => {
    event.preventDefault(); if (busy || !operation) return;
    const submitted = {...operation};
    const current = sceneOperation(submitted.no);
    if (current && current.state !== 'failed') return;
    const body = new FormData();
    Object.entries({...submitted, revision:data.library.revision}).forEach(([key, value]) => body.append(key, value));
    body.append('instruction', $('instruction').value); body.append('count', $('variant-count').value);
    if ($('suasi-pose') && !$('pose-field').hidden) body.append('suasi_pose', $('suasi-pose').value);
    if ($('image-file').files[0]) body.append('image', $('image-file').files[0]);
    $('variant-dialog').close(); operation = null;
    const slow = submitted.kind === 'generate' || submitted.kind === 'edit';
    if (slow) {
      // Only this scene's generation controls wait. Adoption and other scenes
      // remain available, and the server keeps its existing resource limits.
      for (const [id, op] of pending) if (String(op.no) === String(submitted.no) && op.state === 'failed') pending.delete(id);
      const op = {request_id:submitted.request_id, no:submitted.no, kind:submitted.kind, state:'submitting'};
      pending.set(op.request_id, op); savePending(); updateOperations();
      runPending(op, body);
      return;
    }
    setBusy(true);
    $('operation-state').hidden = false; $('operation-state').textContent = '候補を作成しています。このままお待ちください。';
    // 生成・手直しは時間がかかるので「受け付け → 問い合わせ」で待つ（左右反転・取り込みはすぐ終わるので今までどおり）
    try { applyPayload(await Studio.api(base + '/variant', {method:'POST', body})); Studio.notice('候補を追加しました。比べてから採用できます。'); }
    catch (error) { Studio.notice(error.message, true); }
    finally { setBusy(false); await refresh(); }
  });
  document.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => $(button.dataset.close).close()));
  $('flag-form').addEventListener('submit', event => {
    event.preventDefault(); if (busy || !$('flag-note').value.trim()) return;
    $('flag-dialog').close(); select(flagNo, 'flag', undefined, $('flag-note').value.trim());
  });
  $('clear-flag').addEventListener('click', () => { if (!busy) { $('flag-dialog').close(); select(flagNo, 'unflag'); } });
  document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => {
    filter = button.dataset.filter;
    draw(); savePosition();
  }));
  $('search').addEventListener('input', () => { draw(); savePosition(); });
  $('chapter-filter').addEventListener('change', () => { draw(); savePosition(); });
  $('clear-view').addEventListener('click', () => { filter = 'all'; $('search').value = ''; $('chapter-filter').value = ''; draw(); savePosition(); });
  $('restore-view').addEventListener('click', () => {
    if (!lastVisit) return;
    filter = lastVisit.filter; $('search').value = lastVisit.query; $('chapter-filter').value = lastVisit.chapter;
    draw(); restorePosition(lastVisit);
  });
  let positionTimer;
  window.addEventListener('scroll', () => { clearTimeout(positionTimer); positionTimer = setTimeout(savePosition, 250); }, {passive:true});
  window.addEventListener('pagehide', savePosition);
  $('refresh').addEventListener('click', refresh);
  $('undo').addEventListener('click', () => { if (data?.rows.length) select(data.rows[0].no, 'undo'); });
  let exportPlan = null;
  async function downloadSelection() {
    if (!exportPlan || busy) return;
    $('export-dialog').close(); setBusy(true);
    try {
      const response = await fetch(base + '/export', {method:'POST', headers:{'Content-Type':'application/json', 'X-Material-CSRF':document.querySelector('meta[name="material-csrf"]').content},
        body:JSON.stringify({revision:exportPlan.material_revision, selection_hash:exportPlan.selection_hash})});
      if (!response.ok) { const error = await response.json().catch(() => ({})); throw new Error(error.error || '書き出しに失敗しました'); }
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement('a'); anchor.href = url; anchor.download = 'materials_' + window.materialJobId + '.zip';
      document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000);
      applyPayload(await Studio.api(base)); Studio.notice('採用素材を書き出しました。');
    } catch (error) { Studio.notice(error.message, true); }
    finally { setBusy(false); }
  }
  $('download').addEventListener('click', async event => {
    event.preventDefault(); if (busy) return;
    setBusy(true);
    try {
      exportPlan = await Studio.api(base + '/handoff');
      setBusy(false);
      const openComments = exportPlan.review_comments.open;
      if (!exportPlan.ready_for_editing || openComments) {
        const count = new Set(exportPlan.missing.map(item => item.no)).size;
        $('export-title').textContent = exportPlan.ready_for_editing ? '採用素材を書き出します' : '途中の採用素材を書き出します';
        $('export-confirm').textContent = exportPlan.ready_for_editing ? '採用素材を書き出す' : '途中版として書き出す';
        $('export-warning').textContent = (count ? '未採用・要修正など、確認が必要な場面が ' + count + ' 件あります。' : '') +
          (exportPlan.source_job_status !== 'completed' ? '生成処理もまだ完了していません。' : '') +
          (openComments ? '未対応のコメントが ' + openComments + ' 件あります。' : '');
        $('export-dialog').showModal();
      } else await downloadSelection();
    } catch (error) { Studio.notice(error.message, true); }
    finally { setBusy(false); }
  });
  $('export-confirm').addEventListener('click', downloadSelection);
  comments.start();
  // Open the saved candidates for a read-only spot check without importing images.
  if (new URLSearchParams(location.search).get('view') === 'saved') {
    Studio.api(base, {cache:'no-store'}).then(next => applyPayload(next)).catch(error => Studio.notice(error.message, true));
  } else refresh();
})();
