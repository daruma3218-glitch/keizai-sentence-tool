const {test} = require('node:test');
const assert = require('node:assert/strict');
const status = require('../static/studio_status.js');
const view = require('../static/material_view.js');

test('generation failure, review flags, active retries and unknown states stay distinct', () => {
  assert.equal(status.row('failed', true), 'failed');
  assert.equal(status.row('ok', true), 'attention');
  assert.equal(status.row('generating', true), 'generating');
  assert.equal(status.describe('unknown-future-state').tone, 'neutral');
  assert.equal(status.describe('constructor').tone, 'neutral');
  assert.equal(status.chapter({total:4, ok:2, finished:4}), 'completed');
  assert.equal(status.chapter({total:4, ok:2, finished:4, issue:1}), 'attention');
  assert.ok(!status.html('completed', '<img onerror=evil()>').includes('<img'));
});
const lib = {display:{'4':'none'}, selections:{'1':'chosen','4':'old-adoption'}, flags:{'1':'Please fix'}, candidates:{}};
const rows = [
  {no:1, chapter_index:1, sentence:'Same first scene', display:'image'},
  {no:2, chapter_index:1, sentence:'Second scene', display:'hold'},
  {no:3, chapter_index:2, sentence:'Third scene', display:'image'},
  {no:4, chapter_index:2, sentence:'Fourth scene', display:'image'},
  {no:5, sentence:'Legacy scene', display:'image'},
];
test('chapter and adoption filters are scene based and do not count held/hidden images', () => {
  const select = settings => rows.filter(row => view.matches(row, lib, {filter:'all',query:'',chapter:'',...settings})).map(row => row.no);
  assert.deepEqual(select({filter:'pending'}), [3,5]);
  assert.deepEqual(select({filter:'pending',chapter:'i:2'}), [3]);
  assert.deepEqual(select({filter:'selected'}), [1]);
  assert.deepEqual(select({filter:'flagged'}), [1]);
  assert.deepEqual(select({chapter:'unknown'}), [5]);
  assert.deepEqual(select({chapter:'i:1',query:'SECOND'}), [2]);
  assert.equal(view.state(rows[0], lib), 'flagged'); // Adoption never clears a manual flag.
  assert.equal(view.state(rows[3], lib), 'none');
  assert.deepEqual(lib.selections, {'1':'chosen','4':'old-adoption'});
});
test('old or malformed browser state has bounded, usable defaults', () => {
  assert.equal(view.saved(null), null);
  assert.deepEqual(view.saved({filter:'invalid',chapter:12,query:null,no:{},offset:Infinity,y:-10}),
    {filter:'all',chapter:'',query:'',no:'',offset:0,y:0});
});
test('progress buckets are exclusive even when active/failed/completed rows carry flags', () => {
  const rows = [{status:'failed',flag:true}, {status:'generating',flag:true}, {status:'ok',flag:true},
    {status:'ok'}, {status:'pending'}, {status:'skipped'}, {status:'new-state'}];
  const counts = status.summary(rows, r => !!r.flag);
  assert.deepEqual(counts, {pending:1,generating:1,ok:1,attention:1,failed:1,other:2});
  assert.equal(Object.values(counts).reduce((a,b) => a+b,0), rows.length);
});

test('next unadopted scene stays in chapter/search scope, wraps once and excludes current scene', () => {
  const scope = {chapter:'i:2', query:'', filter:'selected'};
  assert.equal(view.nextPending(rows, lib, '', scope).no, 3);
  assert.equal(view.nextPending(rows, lib, 5, scope).no, 3);
  assert.equal(view.nextPending(rows, lib, 3, scope), null);
  assert.equal(view.nextPending(rows, lib, '', {...scope, query:'absent'}), null);
  assert.equal(view.nextPending(rows, lib, 3, {chapter:'',query:'',filter:'all'}).no, 5);
  assert.deepEqual(lib.selections, {'1':'chosen','4':'old-adoption'});
});

test('ready-looking adoption counts do not hide unresolved flags, missing candidates or invalid holds', () => {
  const scenes = [
    {no:1,chapter_index:1,display:'hold'},
    {no:2,chapter_index:1,display:'image'},
    {no:3,chapter_index:1,display:'hold'},
    {no:4,chapter_index:2,display:'hold'},
    {no:5,chapter_index:2,display:'none'},
    {no:6,chapter_index:2,display:'hold'},
    {no:7,chapter_index:2,display:'image'},
  ];
  const library = {display:{}, flags:{'3':'check'}, selections:{'2':'ok','7':'lost'}, candidates:{'2':[{id:'ok'}]}};
  assert.deepEqual(view.remaining(scenes, library).map(r => r.no), [1,3,4,6,7]);
  library.flags = {};
  assert.deepEqual(view.remaining(scenes, library).map(r => r.no), [1,4,6,7]);
});
