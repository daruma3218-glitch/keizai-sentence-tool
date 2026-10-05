'use strict';
(function(root) {
  const definitions = {
    queued:{tone:'queued',label:'順番待ち',icon:'◷'},
    pending:{tone:'queued',label:'生成待ち',icon:'◷'},
    running:{tone:'working',label:'生成中',icon:'↻'},
    generating:{tone:'working',label:'生成中',icon:'↻'},
    completed:{tone:'done',label:'生成完了',icon:'✓'},
    ok:{tone:'done',label:'生成済み',icon:'✓'},
    adopted:{tone:'done',label:'採用済み',icon:'✓'},
    selection_pending:{tone:'queued',label:'採用待ち',icon:'◷'},
    attention:{tone:'attention',label:'要確認',icon:'!'},
    flagged:{tone:'attention',label:'要修正',icon:'!'},
    error:{tone:'error',label:'処理エラー',icon:'×'},
    failed:{tone:'error',label:'生成失敗',icon:'×'},
    interrupted:{tone:'attention',label:'中断',icon:'Ⅱ'},
    cancelled:{tone:'neutral',label:'停止済み',icon:'Ⅱ'},
    skipped:{tone:'neutral',label:'スキップ',icon:'−'},
    thinned:{tone:'neutral',label:'間引き',icon:'−'},
    skipped_limit:{tone:'neutral',label:'間引き',icon:'−'},
    hold:{tone:'neutral',label:'前の素材を継続',icon:'→'},
    none:{tone:'neutral',label:'素材なし',icon:'−'},
    loading:{tone:'neutral',label:'読み込み中',icon:'…'},
    unknown:{tone:'neutral',label:'状態不明',icon:'?'}
  };
  const escape = value => String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const describe = status => Object.prototype.hasOwnProperty.call(definitions, status) ? definitions[status] : definitions.unknown;
  const content = (status, label) => '<span class="status-icon" aria-hidden="true">' + describe(status).icon + '</span><span>' + escape(label || describe(status).label) + '</span>';
  const html = (status, label) => '<span class="status-badge" data-state="' + (Object.hasOwn(definitions, status) ? status : 'unknown') + '" data-tone="' + describe(status).tone + '">' + content(status, label) + '</span>';
  function apply(element, status, label) {
    element.classList.add('status-badge');
    element.dataset.tone = describe(status).tone;
    element.dataset.state = Object.hasOwn(definitions, status) ? status : 'unknown';
    element.innerHTML = content(status, label);
  }
  function row(status, issue) { return ['failed','generating'].includes(status) ? status : issue ? 'attention' : status || 'pending'; }
  function chapter(stats) {
    if (stats.failed) return 'failed';
    if (stats.issue) return 'attention';
    if (stats.generating) return 'generating';
    return stats.total > 0 && (stats.finished ?? stats.ok) === stats.total ? 'completed' : 'pending';
  }
  function summary(rows, hasIssue) {
    const counts = {pending:0, generating:0, ok:0, attention:0, failed:0, other:0};
    rows.forEach(item => {
      const value = row(item.status, hasIssue(item));
      const key = ['pending','queued'].includes(value) ? 'pending' : Object.hasOwn(counts, value) ? value : 'other';
      counts[key] += 1;
    });
    return counts;
  }
  const api = {describe, content, html, apply, row, chapter, summary};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.StudioStatus = api;
})(typeof window === 'object' ? window : {});
