const {test} = require('node:test');
const assert = require('node:assert/strict');
const view = require('../static/material_view.js');

test('hidden candidates leave the default review route and can be explicitly shown', () => {
  const lib = {candidates:{'1':[{id:'original'},{id:'alternate'}]}, hidden_candidates:{original:true}};
  assert.deepEqual(view.candidates(lib, 1).map(c => c.id), ['alternate']);
  assert.deepEqual(view.candidates(lib, 1, true).map(c => c.id), ['original','alternate']);
  assert.equal(lib.candidates['1'].length, 2);
  lib.hidden_candidates.alternate = true;
  assert.deepEqual(view.candidates(lib, '1'), []);
  delete lib.hidden_candidates.original;
  assert.deepEqual(view.candidates(lib, 1).map(c => c.id), ['original']);
});

test('older saved libraries and scenes without candidates stay readable', () => {
  assert.deepEqual(view.candidates({candidates:{'1':[{id:'original'}]}}, 1), [{id:'original'}]);
  assert.deepEqual(view.candidates({candidates:{}}, 2), []);
});
