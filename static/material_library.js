'use strict';
(() => {
  const $ = id => document.getElementById(id), esc = Studio.escape;
  const base = '/api/material-jobs/' + encodeURIComponent(window.materialJobId);
  let data = null, filter = 'all', busy = false, operation = null;
  const src = candidate => '/materials/asset/' + encodeURIComponent(window.materialJobId) + '/' + encodeURIComponent(candidate.id);
  const display = row => data.library.display[String(row.no)] || row.display || (row.route === 'skip' ? 'none' : 'image');
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
    const statuses = {completed:'制作完了',running:'素材を制作中',queued:'制作の順番待ち',failed:'制作エラー',interrupted:'制作が中断されています',cancelled:'制作を停止済み'};
    $('job-status').textContent = ' · ' + (statuses[data.status] || '制作状況を確認してください');
    const query = $('search').value.trim().toLowerCase();
    const visible = rows.filter(row => {
      const no = String(row.no);
      return (!query || row.sentence.toLowerCase().includes(query)) &&
        (filter === 'all' || filter === 'pending' && display(row) === 'image' && !lib.selections[no] ||
         filter === 'selected' && !!lib.selections[no] || filter === 'flagged' && !!lib.flags[no]);
    });
    $('rows').innerHTML = visible.map(row => {
      const no = String(row.no), candidates = lib.candidates[no] || [], selected = lib.selections[no], mode = display(row);
      return '<article class="scene" data-no="' + esc(no) + '"><div class="scene-caption"><div><span class="scene-number">' +
        esc(String(row.no).padStart(3,'0')) + '</span><span class="pill">' + esc(row.chapter_title || ('第' + (row.chapter_index ?? 1) + '章')) +
        '</span>' + (lib.flags[no] ? '<span class="flag-label">要修正</span>' : '') +
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
    }).join('') || '<div class="empty"><p>' + (rows.length ? '該当する場面がありません。' : '原稿を解析中、またはまだ素材がありません。「制作の進み具合」から確認できます。') + '</p></div>';
    const operations = Object.values(lib.requests);
    const unsettled = operations.filter(op => op.status === 'running');
    $('operation-state').hidden = !unsettled.length;
    $('operation-state').textContent = '候補を作成中、または処理結果を確認中です。通信が切れた場合も、まず「素材を読み込む」で結果を確認してください。';
    const requested = operations.reduce((n, op) => n + (op.requested_images || 0), 0);
    $('cost-note').textContent = 'この画面からの追加生成：' + requested + ' 案を依頼。金額は未集計です。利用先の請求と照合して振り返ります。';
    setBusy(busy);
  }
  async function refresh() {
    if (busy) return;
    setBusy(true);
    try { data = await Studio.api(base + '/sync', {method:'POST'}); draw(); }
    catch (error) { Studio.notice(error.message, true); }
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
      const note = prompt('修正したい内容を入力してください。', data.library.flags[no] || '');
      if (note !== null) select(no, note.trim() ? 'flag' : 'unflag', id, note);
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
  document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => {
    filter = button.dataset.filter;
    document.querySelectorAll('[data-filter]').forEach(b => b.classList.toggle('active', b === button)); draw();
  }));
  $('search').addEventListener('input', draw);
  $('refresh').addEventListener('click', refresh);
  $('undo').addEventListener('click', () => { if (data?.rows.length) select(data.rows[0].no, 'undo'); });
  refresh();
})();
