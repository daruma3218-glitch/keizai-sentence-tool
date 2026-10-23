/* Google tokens remain in this closure, never in server requests or browser storage. */
(() => {
  const MIME = 'application/vnd.google-apps.spreadsheet';
  const XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
  const SCOPE = 'https://www.googleapis.com/auth/drive.file';
  const API = 'https://www.googleapis.com/drive/v3/files';
  const UPLOAD = 'https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=id,mimeType,webViewLink';

  function sheetLink(file) {
    if (file?.mimeType !== MIME || !file.id) throw new Error('Googleスプレッドシートへの変換を確認できませんでした。Driveで確認してください。');
    const link = new URL(file.webViewLink || '');
    if (link.protocol !== 'https:' || link.hostname !== 'docs.google.com' || !link.pathname.startsWith('/spreadsheets/d/')) throw new Error('作成先のURLを確認できませんでした。Driveで確認してください。');
    return link.href;
  }

  async function checked(response) {
    if (response.ok) return response;
    if (response.status === 401) throw new Error('Googleの認証期限が切れました。Driveで作成状況を確認してから、もう一度操作してください。');
    if (response.status === 403) throw new Error('Googleへの保存が許可されていません。Driveの空き容量と、このアプリの許可を確認してください。');
    throw new Error('Googleへの保存結果を確認できませんでした。重複作成を防ぐため、Driveで「素材レビュー」を確認してください。');
  }

  async function upload(file, title, accessToken, attemptId, fetcher = fetch) {
    const authorization = 'Bearer ' + accessToken;
    const start = await checked(await fetcher(UPLOAD, {
      method:'POST', headers:{Authorization:authorization, 'Content-Type':'application/json',
        'X-Upload-Content-Type':XLSX, 'X-Upload-Content-Length':String(file.size)},
      body:JSON.stringify({name:title, mimeType:MIME, appProperties:{materialReviewExport:attemptId}})
    }));
    const location = new URL(start.headers.get('Location') || '');
    if (location.protocol !== 'https:' || location.hostname !== 'www.googleapis.com' || !location.pathname.startsWith('/upload/drive/')) throw new Error('Googleの保存先を確認できませんでした。');
    // No automatic retry: a lost completion response must not create a second file.
    const finish = await checked(await fetcher(location.href, {method:'PUT', headers:{Authorization:authorization, 'Content-Type':XLSX}, body:file}));
    const created = await finish.json();
    if (!/^[a-zA-Z0-9_-]+$/.test(created.id || '')) throw new Error('作成結果を確認できませんでした。Driveを確認してください。');
    const verify = await checked(await fetcher(API + '/' + created.id + '?fields=id,mimeType,webViewLink', {headers:{Authorization:authorization}}));
    return sheetLink(await verify.json());
  }

  if (typeof module !== 'undefined' && module.exports) { module.exports = {upload, sheetLink}; return; }
  document.querySelectorAll('[data-review-export]').forEach(panel => {
    const button = panel.querySelector('[data-sheet-create]');
    const status = panel.querySelector('[data-sheet-status]');
    const open = panel.querySelector('[data-sheet-open]');
    const clientId = panel.dataset.googleClientId;
    if (!clientId) return;
    let client, busy = false;
    const say = message => { status.textContent = message; };
    function release() { busy = false; button.disabled = false; }
    function initialise() {
      client = google.accounts.oauth2.initTokenClient({client_id:clientId, scope:SCOPE,
        include_granted_scopes:false,
        error_callback:() => { say('Googleの画面が閉じられました。シートは作成していません。'); release(); },
        callback:async result => {
          if (result.error || !result.access_token || !google.accounts.oauth2.hasGrantedAllScopes(result, SCOPE)) {
            say('Googleへの保存許可が得られませんでした。シートは作成していません。'); release(); return;
          }
          try {
            say('画像プレビューと全場面の一覧を準備しています…');
            const response = await fetch('/api/material-jobs/' + encodeURIComponent(panel.dataset.jobId) + '/review.xlsx', {
              method:'POST', credentials:'same-origin', headers:{'X-Material-CSRF':document.querySelector('meta[name="material-csrf"]').content}
            });
            if (!response.ok) {
              const error = await response.json().catch(() => ({}));
              throw new Error(error.error || '素材一覧を準備できませんでした。ページを読み込み直してください。');
            }
            const title = decodeURIComponent(response.headers.get('X-Review-Title') || '素材') + '｜素材レビュー ' + new Date().toLocaleString('ja-JP');
            const file = await response.blob();
            say('Googleスプレッドシートへ保存しています…');
            const url = await upload(file, title, result.access_token, crypto.randomUUID());
            open.href = url; open.hidden = false;
            say('作成しました。「作成したシートを開く」から、黄色のコメント欄へ記入できます。共有相手はGoogle側で選んでください。');
            open.focus();
          } catch (error) {
            say(error instanceof TypeError ? '通信が切れ、作成結果を確認できませんでした。再操作の前にDriveで「素材レビュー」を確認してください。' : error.message);
          } finally { result.access_token = ''; release(); }
        }});
      button.disabled = false;
    }
    button.disabled = true;
    if (window.google?.accounts?.oauth2) initialise();
    else {
      const script = document.createElement('script'); script.src = 'https://accounts.google.com/gsi/client'; script.async = true;
      script.onload = initialise;
      script.onerror = () => say('Googleの連携画面を読み込めませんでした。ページを読み込み直してください。');
      document.head.appendChild(script);
    }
    button.addEventListener('click', () => {
      if (busy || !client) return;
      busy = true; button.disabled = true;
      say('保存先のGoogleアカウントを選んでください。');
      try { client.requestAccessToken({prompt:'select_account'}); }
      catch (_) { say('Googleの画面を開けませんでした。ポップアップの許可を確認してください。'); release(); }
    });
  });
})();
