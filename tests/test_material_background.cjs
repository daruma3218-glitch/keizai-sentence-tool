const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const tick = () => new Promise(resolve => setImmediate(resolve));

function fixture(saved = []) {
  const elements = new Map(), calls = [], work = [], resumes = [], notices = [];
  const storage = new Map([['material.pending.v1.sample', JSON.stringify(saved)]]);
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value:'', textContent:'', innerHTML:'', hidden:false, disabled:false, open:false,
      dataset:{}, files:[], events:{}, classList:{toggle() {}, add() {}, remove() {}},
      addEventListener(name, handler) { this.events[name] = handler; },
      setAttribute() {}, removeAttribute() {}, focus() {},
      showModal() { this.open = true; }, close() { this.open = false; },
      getBoundingClientRect() { return {top:0, bottom:100}; },
      closest() { return null; }, querySelectorAll() { return []; }
    });
    return elements.get(id);
  }
  const scenes = [1,2,3].map(no => {
    const scene = element('scene-' + no); scene.dataset.no = String(no);
    const actions = ['generate','edit','flip','upload','select','preview','compare'].map(action => {
      const button = element(no + '-' + action); button.dataset.action = action;
      button.closest = selector => selector === '.scene' ? scene : selector === 'button[data-action]' ? button : null;
      return button;
    });
    scene.querySelector = selector => selector === '.scene-operation' ? element(no + '-status') : null;
    scene.querySelectorAll = selector => selector.includes('data-action') ? actions.slice(0,4) : [];
    scene.actions = actions;
    return scene;
  });
  let latest = {status:'completed', rows:[1,2,3].map(no => ({no, chapter_index:1, sentence:'Scene ' + no, display:'image'})),
    library:{revision:1,selection_revision:1,selections:{},flags:{},display:{},requests:{},
      candidates:{'1':[{id:'one',label:'Original'}],'2':[{id:'two',label:'Original'}]}}};
  const copy = value => JSON.parse(JSON.stringify(value));
  const context = vm.createContext({console, FormData, crypto:require('node:crypto').webcrypto,
    URLSearchParams, location:{search:''},
    MaterialComments:{mount:() => ({openScenes:new Map(), label:() => 'コメント', start() {}, selectImage() {}, variant() {}})},
    setTimeout:() => 0, clearTimeout() {}, requestAnimationFrame:fn => fn(),
    localStorage:{getItem:key => storage.get(key), setItem:(key,value) => storage.set(key,value)},
    window:{materialJobId:'sample',scrollY:0,scrollTo() {},addEventListener() {}},
    document:{getElementById:element,activeElement:null,
      querySelectorAll(selector) { return selector === '.scene' ? scenes : selector.startsWith('#rows button') ? [...scenes.flatMap(s => s.actions), element('review-next')] : []; }},
    MaterialView:require('../static/material_view.js'), StudioStatus:require('../static/studio_status.js'),
    Studio:{escape:value => String(value ?? ''), notice:(...args) => notices.push(args),
      api:async (url, options = {}) => {
        calls.push({url,options});
        if (url.endsWith('/variant')) return new Promise((resolve,reject) => work.push({resolve,reject,options}));
        if (url.endsWith('/selection')) {
          const body = JSON.parse(options.body);
          latest.library.selection_revision += 1; latest.library.revision += 1;
          latest.library.display[body.no] = body.action;
        }
        return copy(latest);
      }},
    StudioOperation:{resume:(id, options) => new Promise((resolve,reject) => resumes.push({id,options,resolve,reject}))}
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/material_library.js'),'utf8'),context);
  async function submit(no) {
    await element('rows').events.click({target:element(no + '-generate')});
    await element('variant-form').events.submit({preventDefault() {}});
    await tick();
  }
  return {element,submit,work,resumes,calls,notices,storage,get latest() {return latest;},set latest(v) {latest=v;}};
}

test('generation leaves review and another scene usable, allows two requests and blocks a duplicate', async () => {
  const f = fixture(); await tick();
  await f.submit(1);
  assert.equal(f.element('1-generate').disabled, true);
  assert.equal(f.element('2-generate').disabled, false);
  assert.equal(f.element('1-select').disabled, false);
  assert.equal(f.element('review-next').disabled, false);
  assert.equal(f.element('variant-dialog').open, false);
  await f.submit(1);
  assert.equal(f.work.length, 1);
  await f.submit(2);
  assert.equal(f.work.length, 2);
  f.work[1].options.onWait({waiting:true});
  assert.match(f.element('2-status').textContent, /順番待ち/);
  f.element('review-next').events.click();
  assert.equal(f.element('preview-dialog').open, true);
  assert.equal(f.element('review-next-scene').disabled, false);
});

test('completion reads current adoption instead of an old response and releases only its own scene', async () => {
  const f = fixture(); await tick(); await f.submit(1); await f.submit(2);
  f.latest.library.revision = 6; f.latest.library.selections['2'] = 'two';
  f.work[0].resolve({library:{revision:2,selections:{}}}); await tick(); await tick();
  assert.equal(f.element('1-generate').disabled, false);
  assert.equal(f.element('2-generate').disabled, true);
  assert.match(f.element('adoption-count').textContent, /^1 \/ 3/);
  assert.ok(f.calls.some(call => call.url === '/api/material-jobs/sample' && call.options.cache === 'no-store'));
});

test('reloading pending work resumes polling with no generation POST', async () => {
  const id = 'c'.repeat(32);
  const f = fixture([{request_id:id,no:'1',kind:'generate'}]); await tick();
  assert.equal(f.resumes.length, 1, JSON.stringify(f.notices)); assert.equal(f.resumes[0].id, id);
  assert.equal(f.work.length, 0);
  assert.equal(f.element('2-generate').disabled, false);
});

test('failure stays on its scene and does not cancel another request or retry a paid generation', async () => {
  const f = fixture(); await tick(); await f.submit(1); await f.submit(2);
  f.work[0].reject(Object.assign(new Error('生成に失敗しました'),{status:409})); await tick(); await tick();
  assert.match(f.element('1-status').textContent,/生成に失敗/);
  assert.equal(f.element('1-generate').disabled, false);
  assert.equal(f.element('2-generate').disabled, true);
  assert.equal(f.work.length, 2);
});

test('adoption request carries its independent selection revision during background work', async () => {
  const f = fixture(); await tick(); await f.submit(1);
  f.element('review-next').events.click();
  await f.element('review-adopt').events.click();
  const request = f.calls.find(call => call.url.endsWith('/selection'));
  assert.equal(JSON.parse(request.options.body).selection_revision, 1);
  assert.equal(f.element('1-generate').disabled, true);
  assert.equal(f.element('2-generate').disabled, false);
});
