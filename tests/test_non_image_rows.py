"""The Oct 20 no-prompt incident must not erase assets or hide real failures."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as appmod
from row_generation import non_image_status, repair_false_failures
from scripts.repair_non_image_rows import repair_job


def row(no, display="hold", **extra):
    return {"no": no, "display": display, "route": "illustration", "status": "failed",
            "error": f"№{no} のAI生成用プロンプトが見つかりません（chart/map は数値・地名のある文のみ再生成可）",
            "regen_finished_at": "2026-10-20T13:00:00", **extra}


def test_repair_only_known_false_failures_and_preserve_everything_else(tmp_path):
    original = {"rows": [
        row(1), row(2, "none"), row(3, "image"),
        row(4, filename="4.png"), row(5, error="API failed"),
        row(6, verify_issue=True), row(7), row(8, prompt="intentional image"),
    ], "custom_metadata": {"adopted": 4}}
    prompts = [{"no": 7, "prompt": "a requested image"}]
    expected = copy.deepcopy(original)
    for item in expected["rows"][:2]:
        item.update(status=item["display"], error="")
    (tmp_path / "rows_progress.json").write_text(json.dumps(original), encoding="utf-8")
    (tmp_path / "prompts.json").write_text(json.dumps({"rows": prompts}), encoding="utf-8")
    (tmp_path / "job.json").write_text('{"status":"completed"}', encoding="utf-8")
    (tmp_path / "manuscript.txt").write_text("原稿は保持", encoding="utf-8")
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "4.png").write_bytes(b"existing-adopted-image")
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert repair_job(tmp_path)["repaired"] == 2
    assert (tmp_path / "rows_progress.json").read_bytes() == before["rows_progress.json"]
    assert repair_job(tmp_path, apply=True)["repaired"] == 2
    assert json.loads((tmp_path / "rows_progress.json").read_text(encoding="utf-8")) == expected
    assert next(tmp_path.glob("*.before-non-image-repair-*.json")).read_bytes() == before["rows_progress.json"]
    for name, data in before.items():
        if name != "rows_progress.json":
            assert (tmp_path / name).read_bytes() == data
    assert repair_job(tmp_path, apply=True)["repaired"] == 0


@pytest.mark.parametrize("display", ["hold", "none"])
def test_bulk_regeneration_of_non_image_does_not_mutate_or_call_api(tmp_path, monkeypatch, display):
    job = tmp_path / "job"
    job.mkdir()
    item = row(1, display, status=display, error="")
    snapshot = json.dumps({"rows": [item]})
    (job / "rows_progress.json").write_text(snapshot, encoding="utf-8")
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(appmod, "APP_PASSWORD", "")

    def forbidden(*args, **kwargs):
        raise AssertionError("non-image row must not reach an API")
    monkeypatch.setattr(appmod, "resolve_channel_keys", forbidden)
    response = appmod.app.test_client().post("/api/regenerate/job/1")
    assert response.status_code == 200
    assert response.json == {"ok": True, "no": 1, "skipped": True, "status": display}
    assert (job / "rows_progress.json").read_text(encoding="utf-8") == snapshot


def test_explicit_route_change_is_still_allowed(tmp_path, monkeypatch):
    job = tmp_path / "job"
    job.mkdir()
    (job / "rows_progress.json").write_text(json.dumps({"rows": [row(1)]}), encoding="utf-8")
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(appmod, "APP_PASSWORD", "")
    monkeypatch.setattr(appmod, "get_channel", lambda _: {"defaults": {}})
    monkeypatch.setattr(appmod, "resolve_channel_keys", lambda _: {})
    monkeypatch.setattr(appmod, "_regenerate_render_chart", lambda *a, **kw: appmod.jsonify({"generated": True}))
    response = appmod.app.test_client().post("/api/regenerate/job/1", data={"force_route": "chart"})
    assert response.status_code == 200 and response.json == {"generated": True}
    assert non_image_status(row(1), {"prompt": "intentional image"}) is None


def test_active_job_repair_refused(tmp_path):
    (tmp_path / "job.json").write_text('{"status":"running"}')
    with pytest.raises(ValueError, match="active job"):
        repair_job(tmp_path, apply=True)


def test_browser_issue_rules():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node only needed for browser helper tests")
    subprocess.run([node, "-e", """
const assert = require('node:assert/strict');
const s = require('./static/studio_status.js');
for (const display of ['hold', 'none']) {
  const r = {display, route:'illustration', status:display};
  assert.equal(s.hasIssue(r,'completed'), false);
  assert.equal(s.hasIssue({...r,status:'failed',error:'API failed'},'completed'), true);
  assert.equal(s.hasIssue({...r,verify_issue:true},'completed'), true);
}
assert.equal(s.hasIssue({display:'image',route:'illustration',status:'pending'},'completed'),true);
assert.equal(s.hasIssue({route:'chart',status:'failed'},'completed'),true);
assert.equal(s.hasIssue({display:'image',route:'web_photo',status:'pending'},'completed'),true);
assert.equal(s.hasIssue({display:'hold',route:'illustration',status:'ok',filename:'1.png'},'completed'),false);
assert.equal(s.summary([{status:'hold',display:'hold',route:'illustration'}],r=>s.hasIssue(r,'completed')).attention,0);
"""], cwd=Path(__file__).resolve().parents[1], check=True)
