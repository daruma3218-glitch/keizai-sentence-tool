const {test} = require('node:test');
const assert = require('node:assert/strict');
const {merge, group, threadHtml, watch} = require('../static/material_comments.js');
const {matches, saved} = require('../static/material_view.js');
const {html} = require('../static/studio_status.js');
const esc = value => html('neutral', value).match(/<span>(.*?)<\/span>/s)[1];
const root = {id:'c1', no:'1', candidate_id:'old', author_name:'<筆者>', body:'<img onerror=alert(1)>',
  mine:true, created_at:'2026-11-03T15:10:00Z', parent_id:null, status:'open'};
const reply = {...root, id:'r1', parent_id:'c1', mine:false, body:'<script>example</script>'};

test('user text, names and replies are escaped; withdrawn text is never rendered', () => {
  const t = group(new Map([[root.id,root],[reply.id,reply]])).threads.get(root.id);
  const content = threadHtml(t, esc);
  assert.match(content, /&lt;img/); assert.match(content, /&lt;筆者&gt;/);
  assert.match(content, /&lt;script&gt;/); assert.doesNotMatch(content, /<img|<script>/);
  assert.match(content, /11\/4 00:10/);
  assert.equal((content.match(/data-comment-action="edit"/g) || []).length, 1);
  const withdrawn = threadHtml({...t, root:{...root,withdrawn:true}}, esc);
  assert.match(withdrawn,/取り消されたコメント/); assert.doesNotMatch(withdrawn,/onerror=/);
});

test('deltas retain other candidates, reject stale responses and reset after a restored file', () => {
  const state = {revision:-1, items:new Map()};
  assert.ok(merge(state,{revision:1,reset:true,comments:[root]},-1));
  assert.ok(merge(state,{revision:2,reset:false,comments:[reply]},1));
  assert.ok(!merge(state,{revision:1,reset:true,comments:[]},-1));
  assert.equal(state.items.size,2);
  assert.ok(merge(state,{revision:3,reset:false,comments:[{...root, status:'resolved'}]},2));
  assert.equal(group(state.items).open,0);
  assert.ok(merge(state,{revision:0,reset:true,comments:[]},3));
  assert.equal(state.items.size,0);
});

test('unresolved filter combines chapter and text and retains replies to withdrawn parents', () => {
  const state = group(new Map([[root.id,{...root, withdrawn:true}],[reply.id,reply]]));
  assert.equal(state.open,1);
  const view = {filter:'comments', query:'図', chapter:'i:2', openScenes:state.openScenes};
  assert.ok(matches({no:1, sentence:'図を確認', chapter_index:2},{},view));
  assert.ok(!matches({no:2, sentence:'図を確認', chapter_index:2},{},view));
  assert.ok(!matches({no:1, sentence:'別の章', chapter_index:1},{},view));
  assert.equal(saved(view).filter,'comments');
  assert.equal(group(new Map([[root.id,{...root, withdrawn:true}]])).open,0);
});

test('polls while visible every 30 seconds, stops in background and catches up on return', () => {
  let change, tick, delay, reads = 0;
  const doc = {hidden:false, addEventListener:(event, fn) => { assert.equal(event,'visibilitychange'); change=fn; }};
  const timers = {setInterval:(fn,ms) => { tick=fn; delay=ms; return 1; }, clearInterval:() => { tick=null; }};
  watch(doc, () => reads++, timers);
  assert.equal(reads,1); assert.equal(delay,30000); tick(); assert.equal(reads,2);
  doc.hidden=true; change(); assert.equal(tick,null); assert.equal(reads,2);
  doc.hidden=false; change(); assert.equal(reads,3); assert.ok(tick);
});
