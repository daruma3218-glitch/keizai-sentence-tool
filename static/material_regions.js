'use strict';

window.MaterialRegions = function (onChange) {
  const byId = id => document.getElementById(id);
  const scope = byId('edit-scope-tools'), overlay = byId('region-overlay');
  const image = byId('variant-preview'), tool = byId('region-tool');
  const instruction = byId('instruction'), feedback = byId('region-feedback');
  const mode = value => scope.querySelector('input[value="' + value + '"]');
  const limit = 8, scale = 1000;
  const deleteText = '囲んだ部分を消し、周囲に合わせて背景を補ってください。';
  let enabled = false, polygons = [], stroke = null, anchor = null, cursor = [500, 500];
  const local = () => enabled && mode('regions').checked;
  const readyImage = () => image.complete && image.naturalWidth > 0;
  const valid = () => !local() || (readyImage() && polygons.length > 0 && !stroke && !anchor);

  function point(event) {
    const box = overlay.getBoundingClientRect();
    return [clamp((event.clientX - box.left) * scale / box.width),
            clamp((event.clientY - box.top) * scale / box.height)];
  }
  function clamp(value) { return Math.max(0, Math.min(scale, Math.round(value))); }
  function rectangle(a, b) { return [a, [b[0], a[1]], b, [a[0], b[1]]]; }
  function shape(tag, attrs) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
    overlay.appendChild(node);
  }
  function draw() {
    overlay.replaceChildren();
    const preview = stroke && (stroke.rect ? rectangle(stroke.points[0], cursor) : stroke.points);
    for (const poly of [...polygons, preview || (anchor && rectangle(anchor, cursor))].filter(Boolean)) {
      shape('polygon', {points:poly.map(p => p.join(',')).join(' '), 'vector-effect':'non-scaling-stroke'});
    }
    if (overlay.matches(':focus-visible')) {
      const [x, y] = cursor;
      shape('path', {d:'M' + (x-14) + ' ' + y + 'h28 M' + x + ' ' + (y-22) + 'v44',
                     'vector-effect':'non-scaling-stroke'});
    }
  }
  function update() {
    const active = local();
    if (enabled) byId('variant-submit').textContent = active ? '部分修正案を作る' : '手直し案を作る';
    overlay.toggleAttribute('hidden', !active);
    byId('region-tools').hidden = byId('region-instructions').hidden = !active;
    byId('region-delete').hidden = !active || !polygons.length || !!instruction.value.trim();
    byId('region-count').textContent = polygons.length + 'か所を選択';
    byId('region-undo').disabled = !polygons.length && !anchor && !stroke;
    byId('region-help').textContent = tool.value === 'rect'
      ? '対角の2点をタップするか、ドラッグして囲みます。続けて別の場所も選べます。'
      : '指やマウスで周りをなぞり、離すと囲みが閉じます。続けて別の場所も選べます。';
    if (active && image.complete && !image.naturalWidth) feedback.textContent = '元画像を読み込めませんでした。画面を読み込み直してください。';
    draw();
    onChange(valid());
  }
  function finish(poly) {
    const area = poly.reduce((n, [x,y], i) => {
      const [px,py] = poly[(i + poly.length - 1) % poly.length];
      return n + x * py - y * px;
    }, 0);
    if (poly.length < 3 || Math.abs(area) < 16) {
      feedback.textContent = '範囲が小さすぎます。少し広めに囲んでください。';
    } else {
      polygons.push(poly);
      feedback.textContent = polygons.length === limit ? '8か所まで選びました。' : '';
    }
    update();
  }
  function canStart() {
    if (!local() || !readyImage()) return false;
    if (polygons.length < limit) return true;
    feedback.textContent = '1回に8か所まで囲めます。「1つ戻す」で選び直せます。';
    return false;
  }
  function cancelStroke() {
    const id = stroke?.id;
    stroke = null;
    anchor = null;
    if (id != null && overlay.hasPointerCapture(id)) overlay.releasePointerCapture(id);
  }
  overlay.addEventListener('pointerdown', event => {
    if (event.button !== 0 || !event.isPrimary || stroke || !canStart()) return;
    event.preventDefault();
    overlay.focus({preventScroll:true});
    cursor = point(event);
    const rect = tool.value === 'rect';
    stroke = {id:event.pointerId, rect, fromAnchor:!!anchor && rect, points:[rect && anchor ? anchor : cursor]};
    anchor = null;
    feedback.textContent = '';
    overlay.setPointerCapture(event.pointerId);
    update();
  });
  function sample(p) {
    const points = stroke.points, prev = points[points.length - 1];
    if (p[0] === prev[0] && p[1] === prev[1]) return;
    points.push(p);
    // Retain the entire outline even during a long pen stroke.
    if (points.length > 128) stroke.points = points.filter((_, i) => i % 2 === 0);
  }
  overlay.addEventListener('pointermove', event => {
    if (stroke?.id !== event.pointerId) return;
    cursor = point(event);
    if (!stroke.rect) sample(cursor);
    draw();
  });
  overlay.addEventListener('pointerup', event => {
    if (stroke?.id !== event.pointerId) return;
    cursor = point(event);
    if (!stroke.rect) sample(cursor);
    const {points, rect, fromAnchor} = stroke;
    cancelStroke();
    if (rect && !fromAnchor && Math.hypot(cursor[0]-points[0][0], cursor[1]-points[0][1]) < 6) {
      anchor = points[0];
      feedback.textContent = '反対側の角をタップしてください。';
      update();
    } else {
      finish(rect ? rectangle(points[0], cursor) : points);
    }
  });
  for (const name of ['pointercancel','lostpointercapture']) overlay.addEventListener(name, event => {
    if (stroke?.id === event.pointerId) { cancelStroke(); update(); }
  });
  overlay.addEventListener('keydown', event => {
    if (event.key === 'Escape' && (anchor || stroke)) {
      event.preventDefault(); cancelStroke(); feedback.textContent = ''; update(); return;
    }
    const moves = {ArrowLeft:[-1,0], ArrowRight:[1,0], ArrowUp:[0,-1], ArrowDown:[0,1]};
    if (moves[event.key]) {
      event.preventDefault();
      cursor = cursor.map((v,i) => clamp(v + moves[event.key][i] * (event.shiftKey ? 50 : 5)));
      draw();
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      if (event.repeat || stroke || !canStart()) return;
      if (anchor) { const rect = rectangle(anchor, cursor); anchor = null; finish(rect); }
      else { anchor = cursor.slice(); feedback.textContent = '反対側の角に移動してEnterを押してください。'; update(); }
    }
  });
  overlay.addEventListener('focus', draw);
  overlay.addEventListener('blur', draw);
  scope.addEventListener('change', () => {
    cancelStroke(); feedback.textContent = '';
    if (!local() && instruction.value === deleteText) instruction.value = '';
    update();
  });
  byId('region-undo').addEventListener('click', () => {
    if (stroke || anchor) cancelStroke(); else polygons.pop();
    feedback.textContent = ''; update();
  });
  byId('region-delete').addEventListener('click', () => {
    if (!instruction.value.trim()) instruction.value = deleteText;
    instruction.focus(); update();
  });
  instruction.addEventListener('input', () => { if (enabled) update(); });
  for (const name of ['load','error']) image.addEventListener(name, () => { if (enabled) update(); });

  return {
    valid,
    reset(active, selectRegion = false) {
      enabled = active;
      cancelStroke(); polygons = []; cursor = [500,500]; feedback.textContent = '';
      mode(selectRegion ? 'regions' : 'whole').checked = true; tool.value = 'lasso'; byId('edit-person').value = 'auto';
      scope.hidden = byId('edit-person-field').hidden = !enabled;
      byId('variant-dialog').classList.toggle('image-editor', enabled);
      update();
    },
    append(body) {
      if (!enabled) return true;
      if (!valid()) { feedback.textContent = '修正したい部分を囲んでください。'; return false; }
      body.append('edit_person', byId('edit-person').value);
      if (local()) { body.append('edit_scope', 'regions'); body.append('regions', JSON.stringify(polygons)); }
      return true;
    }
  };
};
