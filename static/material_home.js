document.getElementById('new-project')?.addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget, button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  const originalLabel = button.textContent;
  button.textContent = '案件を準備しています…';
  form.setAttribute('aria-busy', 'true');
  try {
    const result = await Studio.api('/api/material-projects', {method:'POST', body:JSON.stringify({
      channel_id:form.dataset.channel, title:form.elements.namedItem('title').value, trello_url:form.elements.namedItem('trello_url').value,
      style_id:form.elements.namedItem('style_id')?.value || ''
    })});
    const project = result.project;
    location.href = project.trello_url
      ? '/materials?channel=' + encodeURIComponent(project.channel_id) + '&trello=' + encodeURIComponent(project.trello_url)
      : '/?channel_id=' + encodeURIComponent(project.source_channel_id) + '&project_id=' + encodeURIComponent(project.project_id);
  } catch (error) {
    Studio.notice(error.message, true); button.disabled = false; button.textContent = originalLabel;
    form.removeAttribute('aria-busy');
  }
});

const historyRows = Array.from(document.querySelectorAll('.job-row'));
const historySearch = document.getElementById('history-search');
const historyMore = document.getElementById('history-more');
let historyFilter = 'all', historyLimit = 20;
const historyStorageKey = 'material.history.v1.' + (document.querySelector('[data-history-channel]')?.dataset.historyChannel || '');
try {
  const saved = JSON.parse(sessionStorage.getItem(historyStorageKey) || '{}');
  if (historySearch && typeof saved.query === 'string') historySearch.value = saved.query.slice(0, 300);
  if (['all', 'unfinished', 'completed'].includes(saved.filter)) historyFilter = saved.filter;
} catch (_) { /* Search is available when browser storage is disabled. */ }
function showHistory() {
  if (!historySearch) return;
  const terms = historySearch.value.normalize('NFKC').toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
  let count = 0;
  historyRows.forEach(row => {
    const title = row.dataset.search.normalize('NFKC').toLocaleLowerCase();
    const match = (historyFilter === 'all' || row.dataset.bucket === historyFilter)
      && terms.every(term => title.includes(term));
    row.hidden = !match || ++count > historyLimit;
  });
  document.querySelectorAll('[data-history-filter]').forEach(item => {
    const active = item.dataset.historyFilter === historyFilter;
    item.classList.toggle('active', active);
    item.setAttribute('aria-pressed', String(active));
  });
  document.getElementById('history-no-match').hidden = count > 0;
  document.getElementById('history-count').textContent = Math.min(count, historyLimit) + ' / ' + count + ' 件を表示 · 更新日時は日本時間';
  document.getElementById('history-clear').hidden = !terms.length && historyFilter === 'all';
  historyMore.hidden = count <= historyLimit;
  try { sessionStorage.setItem(historyStorageKey, JSON.stringify({query:historySearch.value, filter:historyFilter})); }
  catch (_) { /* Optional per-tab preference; never affects saved work. */ }
}
document.querySelectorAll('[data-history-filter]').forEach(button => button.addEventListener('click', () => {
  historyFilter = button.dataset.historyFilter;
  historyLimit = 20;
  showHistory();
}));
function clearHistory() {
  historySearch.value = ''; historyFilter = 'all'; historyLimit = 20;
  showHistory(); historySearch.focus();
}
document.querySelectorAll('#history-clear,[data-clear-history]').forEach(button => button.addEventListener('click', clearHistory));
historySearch?.addEventListener('input', () => { historyLimit = 20; showHistory(); });
historySearch?.addEventListener('keydown', event => {
  if (event.key === 'Escape' && historySearch.value) { event.preventDefault(); clearHistory(); }
});
document.addEventListener('keydown', event => {
  if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey || !historySearch
      || event.target.closest('input,textarea,select,[contenteditable="true"],dialog[open]')) return;
  event.preventDefault(); historySearch.focus();
});
historyMore?.addEventListener('click', () => { historyLimit += 20; showHistory(); });
showHistory();

const resumeDialog = document.getElementById('resume-dialog');
let resumeJobId = '';
document.querySelectorAll('.resume-job').forEach(button => button.addEventListener('click', () => {
  resumeJobId = button.dataset.jobId;
  document.getElementById('resume-title').textContent = button.dataset.jobTitle;
  resumeDialog.showModal();
}));
document.querySelectorAll('[data-close-resume]').forEach(button => button.addEventListener('click', () => resumeDialog.close()));
document.getElementById('confirm-resume')?.addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const result = await Studio.api('/api/material-jobs/' + encodeURIComponent(resumeJobId) + '/resume', {method:'POST'});
    location.href = result.redirect;
  } catch (error) { Studio.notice(error.message, true); button.disabled = false; }
});

const projectDialog = document.getElementById('new-project-dialog');
document.querySelectorAll('[data-open-project]').forEach(button => button.addEventListener('click', () => projectDialog.showModal()));
document.querySelector('[data-close-project]')?.addEventListener('click', () => projectDialog.close());
if (projectDialog && (document.getElementById('trello-url').value || document.getElementById('project-title').value || location.hash === '#new-project')) projectDialog.showModal();
