document.getElementById('new-project')?.addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget, button = form.querySelector('button');
  button.disabled = true;
  try {
    const result = await Studio.api('/api/material-projects', {method:'POST', body:JSON.stringify({
      channel_id:form.dataset.channel, title:form.elements.namedItem('title').value, trello_url:form.elements.namedItem('trello_url').value
    })});
    const project = result.project;
    location.href = project.trello_url
      ? '/materials?channel=' + encodeURIComponent(project.channel_id) + '&trello=' + encodeURIComponent(project.trello_url)
      : '/?channel_id=' + encodeURIComponent(project.source_channel_id) + '&project_id=' + encodeURIComponent(project.project_id);
  } catch (error) { Studio.notice(error.message, true); button.disabled = false; }
});

const historyRows = Array.from(document.querySelectorAll('.job-row'));
const historySearch = document.getElementById('history-search');
const historyMore = document.getElementById('history-more');
let historyFilter = 'all', historyLimit = 20;
function showHistory() {
  if (!historySearch) return;
  const query = historySearch.value.normalize('NFKC').toLocaleLowerCase().trim();
  let count = 0;
  historyRows.forEach(row => {
    const match = (historyFilter === 'all' || row.dataset.bucket === historyFilter)
      && row.dataset.search.normalize('NFKC').toLocaleLowerCase().includes(query);
    row.hidden = !match || ++count > historyLimit;
  });
  document.getElementById('history-no-match').hidden = count > 0;
  document.getElementById('history-count').textContent = count ? Math.min(count, historyLimit) + ' / ' + count + ' 件を表示 · 更新日時は日本時間' : '';
  historyMore.hidden = count <= historyLimit;
}
document.querySelectorAll('[data-history-filter]').forEach(button => button.addEventListener('click', () => {
  historyFilter = button.dataset.historyFilter;
  historyLimit = 20;
  document.querySelectorAll('[data-history-filter]').forEach(item => {
    item.classList.toggle('active', item === button);
    item.setAttribute('aria-pressed', String(item === button));
  });
  showHistory();
}));
historySearch?.addEventListener('input', () => { historyLimit = 20; showHistory(); });
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
