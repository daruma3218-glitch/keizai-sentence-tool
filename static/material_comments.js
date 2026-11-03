'use strict';
(function(root) {
  function merge(state, packet, requested) {
    if (requested !== state.revision && packet.revision <= state.revision) return false;
    if (state.revision === packet.revision && !packet.reset) return false;
    if (packet.reset) state.items.clear();
    for (const c of packet.comments) state.items.set(c.id, c);
    state.revision = packet.revision;
    return true;
  }

  function group(items) {
    const threads = new Map(), byImage = new Map(), openScenes = new Map();
    let open = 0;
    for (const c of items.values()) {
      if (!c.parent_id) {
        const thread = {root:c, replies:[]};
        threads.set(c.id, thread);
        if (!byImage.has(c.candidate_id)) byImage.set(c.candidate_id, []);
        byImage.get(c.candidate_id).push(thread);
      }
    }
    for (const c of items.values()) if (c.parent_id) threads.get(c.parent_id)?.replies.push(c);
    for (const t of threads.values()) {
      t.active = !t.root.withdrawn || t.replies.some(r => !r.withdrawn);
      if (t.active && t.root.status === 'open') {
        open++;
        openScenes.set(t.root.no, (openScenes.get(t.root.no) || 0) + 1);
      }
    }
    return {threads, byImage, openScenes, open};
  }

  function watch(doc, refresh, timers) {
    let timer;
    const schedule = () => {
      timers.clearInterval(timer);
      if (!doc.hidden) {
        refresh();
        timer = timers.setInterval(refresh, 30000);
      }
    };
    doc.addEventListener('visibilitychange', schedule);
    schedule();
  }

  const date = new Intl.DateTimeFormat('ja-JP', {timeZone:'Asia/Tokyo', month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit', hourCycle:'h23'});
  function threadHtml(thread, esc) {
    const {root:c, replies, active} = thread, resolved = c.status === 'resolved';
    const button = (action, id, label) => '<button type="button" class="text-button" data-comment-action="' + action + '" data-id="' + esc(id) + '">' + label + '</button>';
    const badge = active ? '<span class="thread-state">' + (resolved ? '対応済み' : '未対応') + '</span>' : '';
    const message = item => '<div class="comment-meta"><b>' + esc(item.author_name) + '</b>' +
      (item.parent_id ? '<span class="reply-tag">返信</span>' : badge) +
      '<time datetime="' + esc(item.created_at) + '">' + esc(date.format(new Date(item.created_at))) + (item.edited_at && !item.withdrawn ? ' · 編集済み' : '') + '</time>' +
      (item.mine && !item.withdrawn ? '<details class="comment-menu" data-menu-id="' + esc(item.id) + '"><summary aria-label="自分のコメントの操作">•••</summary><div>' +
        button('edit', item.id, '直す') + button('withdraw', item.id, '取り消す') + '</div></details>' : '') +
      '</div><p class="comment-text">' + (item.withdrawn ? '取り消されたコメント' : esc(item.body)) + '</p>';
    return '<article class="comment-thread' + (resolved ? ' resolved' : '') + '">' + message(c) +
      replies.map(r => '<div class="comment-reply">' + message(r) + '</div>').join('') +
      (c.status_by ? '<p class="hint">' + esc(c.status_by) + 'が' + (resolved ? '対応済みにしました' : 'もう一度開きました') + ' · ' + esc(date.format(new Date(c.status_at))) + '</p>' : '') +
      '<div class="comment-tools">' + (!c.withdrawn ? button('reply', c.id, '返信') : '') +
      (active ? button(resolved ? 'open' : 'resolved', c.id, resolved ? 'もう一度開く' : '対応済みにする') : '') + '</div></article>';
  }

  function mount({base, sceneCandidates, assetUrl, switchImage, onChange}) {
    const $ = id => document.getElementById(id), esc = Studio.escape;
    const state = {revision:-1, items:new Map()};
    let groups = group(state.items), selected = '', scene = '', mode = 'new', target = '';
    let name = '', nameReady = false, saving = false, loadError = '', variantScene = '';
    const drafts = new Map(), operations = new Map();
    const status = message => { $('comment-status').textContent = message; };
    const setSaving = value => {
      saving = value;
      for (const id of ['comment-submit','comment-name-save','comment-body','comment-name','comment-cancel']) $(id).disabled = value;
    };

    async function refresh() {
      const requested = state.revision;
      try {
        const packet = await Studio.api(base + '/comments' + (requested < 0 ? '' : '?since=' + requested), {cache:'no-store'});
        const recovered = loadError;
        loadError = '';
        if (recovered && $('comment-status').textContent === recovered) status('');
        if (merge(state, packet, requested) || recovered) {
          groups = group(state.items);
          onChange(); renderList(); renderVariant();
        }
      } catch (error) {
        loadError = 'コメントの読み込みに失敗しました。' + error.message;
        $('comment-summary').textContent = loadError;
        $('comment-count').hidden = true;
        status(loadError);
      }
    }
    async function fetchName() {
      const result = await Studio.api('/api/me/display-name', {cache:'no-store'});
      name = result.display_name; nameReady = true; profile();
    }
    function profile() {
      $('comment-author').textContent = name;
      $('comment-rename').hidden = !name;
      $('comment-name-field').hidden = !!name;
      $('comment-name-save').hidden = !name;
      $('comment-name').value = name;
    }
    async function write(path, payload) {
      // Keep the same operation ID when a response is lost and the user retries.
      const key = JSON.stringify([path, payload]);
      if (!operations.has(key)) operations.set(key, crypto.randomUUID());
      const result = await Studio.api(path, {method:'POST', body:JSON.stringify({...payload, request_id:operations.get(key)})});
      operations.delete(key);
      return result;
    }
    async function ensureName() {
      if (!nameReady) await fetchName();
      if (!$('comment-name-field').hidden) {
        const value = $('comment-name').value.trim();
        if (!value) { throw new Error('コメントに表示するお名前を入力してください。'); }
        name = (await write('/api/me/display-name', {display_name:value})).display_name;
        profile();
      }
    }
    function setEditor(nextMode, id, value) {
      mode = nextMode; target = id;
      const label = nextMode === 'reply' ? '返信を書く' : nextMode === 'edit' ? 'コメントを直す' : 'コメントを書く';
      $('comment-label').textContent = label;
      const item = state.items.get(id);
      $('comment-context').hidden = !item || nextMode === 'new';
      $('comment-context').textContent = item ? item.author_name + '「' + item.body + '」' : '';
      $('comment-submit').textContent = nextMode === 'reply' ? '返信する' : nextMode === 'edit' ? '変更を保存' : 'コメントする';
      $('comment-body').value = value;
      $('comment-cancel').hidden = nextMode === 'new';
    }
    function renderList() {
      if (!selected) return;
      const threads = groups.byImage.get(selected) || [];
      const resolved = threads.filter(t => t.root.status === 'resolved');
      $('comment-count').hidden = !threads.length || !!loadError;
      $('comment-count').textContent = threads.length + '件';
      $('comment-show-resolved').closest('label').hidden = !resolved.length;
      const visible = threads.filter(t => t.root.status !== 'resolved' || $('comment-show-resolved').checked);
      $('comment-jump').hidden = !visible.length;
      const list = $('comment-list');
      const menuId = list.querySelector('details[open]')?.dataset.menuId;
      const focus = document.activeElement, focusMenu = focus?.closest('.comment-menu')?.dataset.menuId;
      list.innerHTML = visible.length ? visible.map(t => threadHtml(t, esc)).join('') :
        '<p class="comment-empty">' + esc(loadError || (state.revision < 0 ? 'コメントを読み込んでいます…' : resolved.length ? '未対応のコメントはありません。' : 'この画像にはまだコメントがありません。')) + '</p>';
      for (const menu of list.querySelectorAll('.comment-menu')) {
        if (menu.dataset.menuId === menuId) menu.open = true;
        if (menu.dataset.menuId === focusMenu) {
          const target = [...menu.querySelectorAll('summary,button')].find(el => el.dataset.commentAction === focus.dataset.commentAction);
          target?.focus({preventScroll:true});
        }
      }
      const candidates = sceneCandidates(scene);
      const index = candidates.findIndex(c => c.id === selected);
      $('comment-others').innerHTML = candidates.filter(c => c.id !== selected && groups.byImage.has(c.id)).map(c =>
        '<button type="button" class="other-comment" data-comment-image="' + esc(c.id) + '"><img src="' + esc(assetUrl(c)) + '" alt="">' +
        (candidates.indexOf(c) < index ? '前の候補' : '別の候補') + 'へのコメント（候補' + (candidates.indexOf(c) + 1) + '）</button>').join('');
    }
    function selectImage(no, id) {
      if (selected !== id) {
        if (selected) drafts.set(selected, {mode, target, value:$('comment-body').value});
        selected = id; scene = no;
        const draft = drafts.get(id);
        setEditor(draft?.mode || 'new', draft?.target || '', draft?.value || '');
        $('comment-show-resolved').checked = false;
        status('');
      }
      renderList();
    }
    function renderVariant() {
      const candidates = variantScene ? sceneCandidates(variantScene) : [];
      const entries = candidates.flatMap((c, i) => (groups.byImage.get(c.id) || [])
        .filter(t => t.active && t.root.status === 'open')
        .flatMap(t => [t.root, ...t.replies].filter(m => !m.withdrawn).map(m => ({m, index:i + 1}))));
      $('variant-comments').hidden = !entries.length;
      $('variant-comment-list').innerHTML = entries.map(({m, index}) => '<div class="instruction-comment"><p class="hint">候補' + index + ' · ' +
        esc(m.author_name) + '</p><p class="comment-text">' + esc(m.body) + '</p><button type="button" class="button compact" data-insert="' +
        esc(m.id) + '">指示に入れる</button></div>').join('');
    }
    $('variant-comment-list').addEventListener('click', event => {
      const c = state.items.get(event.target.closest('[data-insert]')?.dataset.insert);
      if (!c || c.withdrawn) return;
      const input = $('instruction'), value = (input.value ? input.value + '\n' : '') + c.body;
      if (value.length > input.maxLength) { $('variant-comment-status').textContent = '指示欄の文字数を超えます。先に内容を短くしてください。'; return; }
      input.value = value; $('variant-comment-status').textContent = '指示欄の末尾に追加しました。'; input.focus();
    });
    $('comment-others').addEventListener('click', event => {
      const id = event.target.closest('[data-comment-image]')?.dataset.commentImage;
      if (id) switchImage(id);
    });
    $('comment-show-resolved').addEventListener('change', renderList);
    $('comment-jump').addEventListener('click', () => $(!name && !$('comment-name-field').hidden ? 'comment-name' : 'comment-body').focus());
    $('comment-rename').addEventListener('click', () => { $('comment-name-field').hidden = false; $('comment-name').focus(); });
    $('comment-cancel').addEventListener('click', () => { setEditor('new', '', ''); status(''); });
    async function perform(task) {
      if (saving) return;
      setSaving(true);
      try { await ensureName(); await task(); }
      catch (error) { status(error.message); }
      finally { setSaving(false); }
    }
    $('comment-name-save').addEventListener('click', () => perform(() => status('表示名を保存しました。')));
    $('comment-submit').addEventListener('click', () => {
      const candidate = selected, no = scene, action = mode, id = target, body = $('comment-body').value.trim();
      if (!candidate || !body) { status('本文を入力してください。'); return; }
      perform(async () => {
        if (action === 'edit') await write(base + '/comments/' + encodeURIComponent(id), {action:'edit', body});
        else await write(base + '/comments', {no, candidate_id:candidate, body, parent_id:action === 'reply' ? id : null});
        drafts.delete(candidate);
        if (selected === candidate) { setEditor('new', '', ''); status('保存しました。'); }
        await refresh();
      });
    });
    $('comment-list').addEventListener('click', event => {
      const button = event.target.closest('[data-comment-action]');
      if (!button || saving) return;
      const {id, commentAction:action} = button.dataset;
      const menu = button.closest('.comment-menu');
      if (menu) menu.open = false;
      if (action === 'reply' || action === 'edit') {
        setEditor(action, id, action === 'edit' ? state.items.get(id).body : '');
        status(''); $('comment-body').focus(); return;
      }
      if (action === 'withdraw' && !window.confirm('このコメントを取り消しますか？ 本文は「取り消されたコメント」に変わります。')) return;
      const path = base + '/comments/' + encodeURIComponent(id);
      perform(async () => {
        if (action === 'withdraw') await write(path, {action});
        else await write(path + '/status', {status:action});
        if (target === id) setEditor('new', '', '');
        status('保存しました。'); await refresh();
      });
    });
    return {
      start() {
        fetchName().catch(error => status(error.message));
        watch(document, refresh, window);
      },
      selectImage,
      variant(no) { variantScene = no; $('variant-comment-status').textContent = ''; renderVariant(); },
      get openScenes() { return groups.openScenes; },
      hasOpen(id) { return (groups.byImage.get(id) || []).some(t => t.active && t.root.status === 'open'); },
      get total() { return groups.open; },
      get summary() { return loadError || (state.revision < 0 ? 'コメントを読み込んでいます…' : '未対応コメント ' + groups.open + ' 件'); },
      label(id) {
        if (state.revision < 0) return 'コメント';
        const threads = groups.byImage.get(id) || [];
        const open = threads.filter(t => t.active && t.root.status === 'open').length;
        return threads.length ? 'コメント ' + threads.length + '件' + (open ? ' · 未対応 ' + open : '') : 'コメント';
      }
    };
  }
  const api = {merge, group, threadHtml, watch, mount};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.MaterialComments = api;
})(typeof window === 'object' ? window : {});
