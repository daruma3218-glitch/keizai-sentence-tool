'use strict';
(() => {
  const $ = id => document.getElementById(id), esc = Studio.escape;
  const base = '/api/material-jobs/' + encodeURIComponent(window.materialJobId);
  let data = null, filter = 'all', busy = false, operation = null, flagNo = '', initialView = true, canSavePosition = false;
  const storageKey = 'material.selection.v1.' + window.materialJobId;
  let lastVisit = null;
  try { lastVisit = MaterialView.saved(JSON.parse(localStorage.getItem(storageKey))); } catch (_) { /* Storage is optional. */ }
  if (lastVisit) { filter = lastVisit.filter; $('search').value = lastVisit.query; $('restore-view').hidden = false; }
  const currentView = () => ({filter, chapter:$('chapter-filter').value, query:$('search').value.trim()});
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
  const display = row => MaterialView.mode(row, data.library);
  function setBusy(value) {
    busy = value;
    document.querySelectorAll('#rows button, #refresh, #undo, #variant-submit').forEach(button => { button.disabled = value; });
  }
  function draw() {
    if (!data) return;
    const lib = data.library, rows = data.rows;
    const required = rows.filter(row => display(row) === 'image');
    const adopted = required.filter(row => lib.selections[String(row.no)]).length;
    $('adoption-count').textContent = adopted + ' / ' + required.length + ' 場面の素材を採用';
    StudioStatus.apply($('job-status'), data.status);
    const exported = data.export?.last;
    $('export-state').textContent = exported ? (data.export.changed ? '前回の書き出し後に変更があります。編集側へ渡すときは、もう一度書き出してください。' : '前回書き出した採用内容と一致しています。') : 'この画面からの書き出し記録はまだありません。旧画面からのダウンロードは記録対象外です。';
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
    $('visible-count').textContent = visible.length + ' / ' + rows.length + ' 場面を表示';
    $('rows').innerHTML = visible.map(row => {
      const no = String(row.no), candidates = lib.candidates[no] || [], selected = lib.selections[no], mode = display(row);
      const selectionState = MaterialView.state(row, lib);
      return '<article class="scene" data-tone="' + StudioStatus.describe(selectionState).tone + '" data-selection-tone data-no="' + esc(no) + '"><div class="scene-caption"><div><span class="scene-number">' +
        esc(String(row.no).padStart(3,'0')) + '</span><span class="pill">' + esc(row.chapter_title || ('第' + (row.chapter_index ?? 1) + '章')) +
        '</span><span class="selection-state">' + StudioStatus.html(selectionState) + '</span>' +
        '</div><p class="sentence">' + esc(row.sentence) + '</p><div class="display-options">' +
        '<span class="hint">表示：</span><button data-action="image" class="' + (mode === 'image' ? 'chosen' : '') + '">素材を切り替え</button><button data-action="hold" class="' + (mode === 'hold' ? 'chosen' : '') + '">前の素材を継続</button>' +
        '<button data-action="none" class="' + (mode === 'none' ? 'chosen' : '') + '">素材なし</button>' +
        '<button data-action="flag">' + (lib.flags[no] ? '修正メモを変更' : '要修正にする') + '</button></div>' +
        (lib.flags[no] ? '<p class="hint flag-label">' + esc(lib.flags[no]) + '</p>' : '') +
        '</div><div class="candidate-strip">' + candidates.map(c => {
          const chosen = c.id === selected;
          return '<div class="candidate ' + (chosen ? 'selected' : '') + '" data-candidate="' + esc(c.id) + '">' +
            '<button class="image-button" data-action="preview" aria-label="候補を拡大"><img loading="lazy" src="' + src(c) + '" alt="' + esc(c.label) + '"></button>' +
            '<div class="candidate-body"><div class="candidate-caption"><strong>' + esc(c.label) + '</strong><span class="hint">' + esc(c.created_at?.slice(0,10)) + '</span></div>' +
            '<button class="button ' + (chosen ? 'adopted' : '') + ' wide" data-action="' + (chosen ? 'clear' : 'select') + '">' + (chosen ? '✓ 採用中（解除する）' : 'この候補を採用') + '</button>' +
            '<div class="candidate-tools"><button data-action="edit">指示して手直し</button><button data-action="flip">左右反転</button></div>' +
            (c.web_source_url ? '<a class="hint" href="' + esc(/^https?:\/\//.test(c.web_source_url) ? c.web_source_url : '#') + '" target="_blank" rel="noopener">出典を確認 ↗</a>' : '') +
            '</div></div>';
        }).join('') + '<div class="add-candidate"><span>＋</span><strong>' + (candidates.length ? '別の案も比べる' : 'この場面の素材を追加') + '</strong>' +
        '<button class="button" data-action="generate">候補を生成</button><button class="text-button" data-action="upload">画像を取り込む</button></div></div></article>';
    }).join('') || '<div class="empty"><p>' + (rows.length ? '該当する場面がありません。絞り込みを解除して確認できます。' : '原稿を解析中、またはまだ素材がありません。「詳しい進捗・手直し」から確認できます。') + '</p></div>';
    const operations = Object.values(lib.requests);
    const unsettled = operations.filter(op => op.status === 'running');
    $('operation-state').hidden = !unsettled.length;
    $('operation-state').dataset.tone = 'working';
    $('operation-state').textContent = '候補を作成中、または処理結果を確認中です。通信が切れた場合も、まず「素材を読み込む」で結果を確認してください。';
    const requested = operations.reduce((n, op) => n + (op.requested_images || 0), 0);
    $('cost-note').textContent = 'この画面からの追加生成：' + requested + ' 案を依頼。金額は未集計です。利用先の請求と照合して振り返ります。';
    setBusy(busy);
    if (initialView) { initialView = false; restorePosition(lastVisit); }
  }
  async function refresh() {
    if (busy) return;
    setBusy(true);
    $('operation-state').hidden = false;
    $('operation-state').dataset.tone = 'working';
    $('operation-state').textContent = '生成済みの画像を候補に取り込んでいます。画像の再生成はしません。';
    try { data = await Studio.api(base + '/sync', {method:'POST'}); draw(); }
    catch (error) { Studio.notice(error.message, true); $('operation-state').dataset.tone = 'attention'; $('operation-state').textContent = error.message; }
    finally { setBusy(false); }
  }
  async function select(no, action, candidate, note) {
    if (busy) return;
    setBusy(true);
    try {
      data = await Studio.api(base + '/selection', {method:'POST', body:JSON.stringify({revision:data.library.revision, no, action, candidate_id:candidate, note})});
      draw(); Studio.notice(action === 'undo' ? '採用操作を戻しました。' : '保存しました。');
    } catch (error) { Studio.notice(error.message, true); }
    finally { setBusy(false); }
  }
  function variant(no, kind, candidate) {
    const row = data.rows.find(row => String(row.no) === no);
    operation = {no, kind, candidate_id:candidate?.id || '', request_id:crypto.randomUUID().replaceAll('-',''), revision:data.library.revision};
    const labels = {generate:'別の候補を作る', edit:'この候補を手直し', flip:'左右を反転した候補を追加', upload:'画像を候補に取り込む'};
    $('variant-title').textContent = labels[kind];
    $('variant-scene').textContent = row.sentence;
    $('instruction').value = ''; $('image-file').value = ''; $('variant-count').value = '1';
    const ai = kind === 'generate' || kind === 'edit';
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
    if (action === 'preview') {
      $('preview-image').src = src(candidate); $('preview-dialog').showModal();
    } else if (['generate','edit','flip','upload'].includes(action)) variant(no, action, candidate);
    else if (action === 'flag') {
      flagNo = no;
      $('flag-note').value = data.library.flags[no] || '';
      $('clear-flag').hidden = !data.library.flags[no];
      $('flag-dialog').showModal();
    } else select(no, action, id);
  });
  $('variant-form').addEventListener('submit', async event => {
    event.preventDefault(); if (busy || !operation) return;
    const body = new FormData();
    Object.entries(operation).forEach(([key, value]) => body.append(key, value));
    body.append('instruction', $('instruction').value); body.append('count', $('variant-count').value);
    if ($('image-file').files[0]) body.append('image', $('image-file').files[0]);
    $('variant-dialog').close(); setBusy(true);
    $('operation-state').hidden = false; $('operation-state').textContent = '候補を作成しています。このままお待ちください。';
    try { data = await Studio.api(base + '/variant', {method:'POST', body}); draw(); Studio.notice('候補を追加しました。比べてから採用できます。'); }
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
      data = await Studio.api(base); draw(); Studio.notice('採用素材を書き出しました。');
    } catch (error) { Studio.notice(error.message, true); }
    finally { setBusy(false); }
  }
  $('download').addEventListener('click', async event => {
    event.preventDefault(); if (busy) return;
    setBusy(true);
    try {
      exportPlan = await Studio.api(base + '/handoff');
      setBusy(false);
      if (!exportPlan.ready_for_editing) {
        const count = new Set(exportPlan.missing.map(item => item.no)).size;
        $('export-warning').textContent = '未採用・要修正など、確認が必要な場面が ' + count + ' 件あります。' +
          (exportPlan.source_job_status !== 'completed' ? '生成処理もまだ完了していません。' : '');
        $('export-dialog').showModal();
      } else await downloadSelection();
    } catch (error) { Studio.notice(error.message, true); }
    finally { setBusy(false); }
  });
  $('export-confirm').addEventListener('click', downloadSelection);
  refresh();
})();
