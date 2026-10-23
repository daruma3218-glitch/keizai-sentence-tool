const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {upload, sheetLink} = require('../static/material_sheet_export.js');
const native = {id:'sample-sheet', mimeType:'application/vnd.google-apps.spreadsheet', webViewLink:'https://docs.google.com/spreadsheets/d/sample-sheet/edit'};

test('upload uses private native conversion then verifies the result', async () => {
  const calls = [];
  const mock = async (url, options) => {
    calls.push({url,options});
    if (calls.length === 1) return new Response('', {status:200,headers:{Location:'https://www.googleapis.com/upload/drive/v3/files?upload_id=one'}});
    return new Response(JSON.stringify(native), {status:200});
  };
  assert.equal(await upload(new Blob(['test']), 'レビュー', 'test-token', 'attempt-1', mock), native.webViewLink);
  assert.equal(calls.length, 3);
  const metadata = JSON.parse(calls[0].options.body);
  assert.equal(metadata.mimeType, native.mimeType);
  assert.equal(metadata.permissions, undefined);
  assert.equal(calls[1].options.method, 'PUT');
  assert.match(calls[2].url, /sample-sheet\?fields=/);
  for (const call of calls) assert.equal(call.url.includes('test-token'), false);
});

test('ambiguous completion is not automatically retried', async () => {
  let calls = 0;
  await assert.rejects(upload(new Blob(['test']), 'レビュー', 'test-token', 'attempt-2', async () => {
    calls++;
    if (calls === 1) return new Response('', {status:200,headers:{Location:'https://www.googleapis.com/upload/drive/v3/files?upload_id=one'}});
    throw new TypeError('connection lost');
  }));
  assert.equal(calls, 2);
});

test('unexpected upload destination does not receive the token', async () => {
  let calls = 0;
  await assert.rejects(upload(new Blob(['test']), 'レビュー', 'secret', 'attempt-3', async () => {
    calls++;
    return new Response('', {status:200,headers:{Location:'https://example.invalid/upload'}});
  }));
  assert.equal(calls, 1);
  assert.throws(() => sheetLink({...native, mimeType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}));
  assert.throws(() => sheetLink({...native, webViewLink:'javascript:alert(1)'}));
});

test('skipped row offers explicit generation and cancellation spends nothing', async () => {
  const source = fs.readFileSync('templates/progress.html', 'utf8');
  const block = source.slice(source.indexOf('    async function generateSkippedScene('), source.indexOf('    // 作り直しは'));
  const calls = [];
  const context = {lastRows:[{no:3,route:'skip'}], prompt:()=>'原稿に沿った図解',
    regenerateSceneWithRoute:(...args)=>{calls.push(args);}, Number};
  vm.createContext(context); vm.runInContext(block, context);
  await context.generateSkippedScene(3, {stopPropagation(){}});
  assert.equal(calls[0][1], 'diagram');
  assert.equal(calls[0][3].extraInstruction, '原稿に沿った図解');
  context.prompt = () => null;
  await context.generateSkippedScene(3);
  assert.equal(calls.length, 1);
  const thumb = source.slice(source.indexOf('    function thumbCellHtml('), source.indexOf('    function webCellHtml('));
  Object.assign(context,{lastJobStatus:'completed', StudioStatus:{row:()=>'',html:()=>''}, rowHasIssue:()=>false, escapeHtml:x=>x});
  vm.runInContext(thumb, context);
  assert.match(context.thumbCellHtml({no:3,status:'skipped',route:'skip'}), /画像を生成/);
  context.lastJobStatus = 'running';
  assert.doesNotMatch(context.thumbCellHtml({no:3,status:'skipped',route:'skip'}), /画像を生成/);
});
