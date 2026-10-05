'use strict';
(function(root) {
  const mode = (row, lib) => lib.display[String(row.no)] || row.display || (row.route === 'skip' ? 'none' : 'image');
  const chapter = row => row.chapter_index != null ? 'i:' + row.chapter_index : row.chapter_title ? 't:' + row.chapter_title : 'unknown';
  function state(row, lib) {
    const no = String(row.no);
    if (lib.flags[no]) return 'flagged';
    if (mode(row, lib) !== 'image') return mode(row, lib);
    return lib.selections[no] ? 'adopted' : 'selection_pending';
  }
  function matches(row, lib, view) {
    const no = String(row.no);
    return (!view.chapter || chapter(row) === view.chapter) &&
      (!view.query || String(row.sentence || '').toLowerCase().includes(view.query.toLowerCase())) &&
      (view.filter === 'all' || view.filter === 'pending' && mode(row, lib) === 'image' && !lib.selections[no] ||
       view.filter === 'selected' && mode(row, lib) === 'image' && !!lib.selections[no] || view.filter === 'flagged' && !!lib.flags[no]);
  }
  function saved(value) {
    if (!value || typeof value !== 'object') return null;
    return {filter:['all','pending','selected','flagged'].includes(value.filter) ? value.filter : 'all',
      chapter:typeof value.chapter === 'string' ? value.chapter.slice(0,200) : '',
      query:typeof value.query === 'string' ? value.query.slice(0,256) : '',
      no:typeof value.no === 'string' ? value.no.slice(0,40) : '',
      offset:Number.isFinite(value.offset) ? Math.max(-5000, Math.min(5000, value.offset)) : 0,
      y:Number.isFinite(value.y) ? Math.max(0, value.y) : 0};
  }
  const api = {mode, chapter, state, matches, saved};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.MaterialView = api;
})(typeof window === 'object' ? window : {});
