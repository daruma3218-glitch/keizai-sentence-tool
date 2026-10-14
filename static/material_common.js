'use strict';
window.Studio = {
  async api(url, options = {}) {
    const {poll, ...init} = options;
    const headers = {'X-Material-CSRF':document.querySelector('meta[name="material-csrf"]').content, ...(init.headers || {})};
    if (typeof init.body === 'string') headers['Content-Type'] = 'application/json';
    // poll: 時間のかかる操作は「受け付け → 問い合わせ」で待つ（新しい入口は応答を約100秒しか待たないため・studio_operation.js）
    const response = poll && window.StudioOperation
      ? await StudioOperation.fetch(url, {...init, headers})
      : await fetch(url, {...init, headers, credentials:'same-origin'});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || (response.status === 503 ? 'サーバーを更新しています。少し待って読み込み直してください。' : '読み込みに失敗しました。再読み込みしてください。'));
    return data;
  },
  notice(message, error = false) {
    const element = document.getElementById('notice');
    element.textContent = message;
    element.classList.toggle('error', error);
    element.hidden = false;
    clearTimeout(this.noticeTimer);
    this.noticeTimer = setTimeout(() => { element.hidden = true; }, error ? 12000 : 5000);
  },
  escape(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  }
};
document.querySelectorAll('[data-studio-status]').forEach(element => StudioStatus.apply(element, element.dataset.studioStatus));
