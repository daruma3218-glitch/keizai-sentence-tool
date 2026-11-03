"""Offline candidate discussions: two signed identities, atomic writes and review exports."""
import csv
import io
import json
import shutil
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from openpyxl import load_workbook
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as appmod
import cloudflare_access as ca
import material_comments as comments
import material_review as review
import material_store as store
from test_cloudflare_access import AUD, CANONICAL, _Keys, token

BASE = "/api/material-jobs/sample"
PEOPLE = "/api/me/display-name"


def operation(**fields):
    return {"request_id": uuid.uuid4().hex, **fields}


@pytest.fixture
def studio(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setenv("CF_ACCESS_AUD", AUD)
    monkeypatch.setattr(ca, "_jwks", lambda _: _Keys())
    d = appmod.OUTPUT_DIR / "sample"
    d.mkdir(parents=True)
    store.save(d / "job.json", {"channel_id":"keizai", "status":"completed"})
    store.save(d / "material_context.json", {"source_channel_id":"keizai", "profile":{}, "title":"試験"})
    store.save(d / "rows_progress.json", {"rows":[{"no":1, "display":"image", "sentence":"文"}]})
    lib = store.load_library(d)
    for color in ["red", "blue"]:
        path = d / (color + ".png")
        Image.new("RGB", (32, 18), color).save(path)
        store.snapshot(d, lib, 1, path, color)
    store.commit_library(d, lib, "fixture")
    ids = [c["id"] for c in lib["candidates"]["1"]]
    client = appmod.app.test_client()
    with client.session_transaction(base_url=CANONICAL) as session:
        session["material_csrf"] = "local-test"
    proofs = {who:token(email=who + "@" + "example.invalid") for who in ("reader", "artist")}

    def call(path, body=None, who="reader", **headers):
        return client.open(path, method="GET" if body is None else "POST", json=body, base_url=CANONICAL,
                           headers={ca.HEADER:proofs[who], "X-Material-CSRF":"local-test", **headers})

    for who, name in [("reader", "確認係"), ("artist", "作り手")]:
        assert call(PEOPLE, operation(display_name=name), who).status_code == 200
    return d, call, ids


def test_existing_job_absence_and_read_only_pages(studio):
    d, call, ids = studio
    before = {p:p.read_bytes() for p in d.rglob("*") if p.is_file()}
    page = call("/materials/jobs/sample?view=saved")
    assert page.status_code == 200 and 'id="image-comments"' in page.text
    assert 'data-sheet-comments' in page.text and 'data-sheet-comments' not in call("/progress/sample").text
    response = call(BASE + "/comments")
    assert response.json == {"comments":[], "revision":0, "reset":True}
    assert response.headers["Cache-Control"] == "private, no-store"
    assert call(BASE + "/handoff").json["review_comments"] == {"open":0,"resolved":0}
    assert before == {p:p.read_bytes() for p in d.rglob("*") if p.is_file()}


def test_profile_is_verified_and_replays_do_not_revert_renames(studio):
    _, call, _ = studio
    first = operation(display_name="新しい名")
    assert call(PEOPLE, first).json == {"display_name":"新しい名"}
    assert call(PEOPLE, operation(display_name="今の名")).status_code == 200
    assert call(PEOPLE, first).json == {"display_name":"新しい名"}
    assert call(PEOPLE).json == {"display_name":"今の名"}
    assert call(PEOPLE, {**first, "display_name":"不一致"}).status_code == 409
    assert call(PEOPLE, who="artist").json == {"display_name":"作り手"}
    assert call(PEOPLE, operation(display_name="名"*41)).status_code == 409


def test_discussion_lifecycle_and_privacy(studio):
    d, call, (old, new) = studio
    before = (d / "material_library.json").read_bytes()
    url = BASE + "/comments"
    create = operation(no=1, candidate_id=old, body="元の指摘", author_name="偽名", mine=False)
    result = call(url, create).json
    assert call(url, create).json == result
    assert call(url, {**create, "body":"別の内容"}).status_code == 409
    parent_url = url + "/" + result["id"]
    reply = operation(no=1, candidate_id=old, body="改善案", parent_id=result["id"])
    reply_id = call(url, reply, "artist").json["id"]
    for bad in [dict(parent_id=reply_id), dict(candidate_id=new), dict(parent_id="absent")]:
        assert call(url, operation(**{**reply, **bad, "request_id":uuid.uuid4().hex})).status_code == 409
    assert call(parent_url, operation(action="edit", body="他人"), "artist").status_code == 409
    assert call(parent_url, operation(action="withdraw"), "artist").status_code == 409
    edit = operation(action="edit", body="矢印の向き")
    assert call(parent_url, edit).json == call(parent_url, edit).json
    rows = call(url, who="artist").json["comments"]
    assert [(c["author_name"], c["mine"]) for c in rows] == [("確認係",False),("作り手",True)]
    assert rows[0]["body"] == "矢印の向き" and rows[0]["edited_at"]
    assert rows[0]["candidate_sha256"] == old.split("-", 1)[1]
    assert "@" not in json.dumps(rows, ensure_ascii=False) and "author_email" not in rows[0]
    resolve = operation(status="resolved")
    assert call(parent_url + "/status", resolve, "artist").json == call(parent_url + "/status", resolve, "artist").json
    diff = call(url + "?since=3").json
    assert not diff["reset"] and [r["id"] for r in diff["comments"]] == [result["id"]]
    assert diff["comments"][0]["resolved_by"] == "作り手" and diff["comments"][0]["resolved_at"]
    assert call(url + "?since=" + str(diff["revision"])).json["comments"] == []
    assert call(url + "?since=9999").json["reset"]
    handoff = call(BASE + "/handoff").json
    assert handoff["schema_version"] == 1 and handoff["review_comments"] == {"open":0, "resolved":1}
    assert "矢印の向き" not in json.dumps(handoff, ensure_ascii=False)
    assert call(parent_url + "/status", operation(status="open")).status_code == 200
    assert call(url + "/" + reply_id + "/status", operation(status="resolved")).status_code == 409
    withdraw = operation(action="withdraw")
    assert call(parent_url, withdraw).json == call(parent_url, withdraw).json
    assert call(parent_url, operation(action="edit", body="戻せない")).status_code == 409
    assert call(url, operation(no=1, candidate_id=old, parent_id=result["id"], body="追加")).status_code == 409
    tombstone = call(url).json["comments"][0]
    assert tombstone["withdrawn"] and tombstone["body"] == ""
    assert "矢印の向き" not in (d / "material_comments.json").read_text(encoding="utf-8")
    assert comments.counts(d) == {"open":1,"resolved":0}  # live reply
    assert call(url + "/" + reply_id, operation(action="withdraw"), "artist").status_code == 200
    assert comments.counts(d) == {"open":0,"resolved":0}
    assert before == (d / "material_library.json").read_bytes()


@pytest.mark.parametrize("endpoint", [PEOPLE, BASE+"/comments", BASE+"/comments/id", BASE+"/comments/id/status"])
def test_new_writes_require_login_csrf_and_same_origin(studio, endpoint):
    _, call, _ = studio
    assert call(endpoint, {}, **{"X-Material-CSRF":"wrong"}).status_code == 403
    assert call(endpoint, {}, **{"Origin":"https://foreign.invalid"}).status_code == 403
    assert call(endpoint, {}, **{"Sec-Fetch-Site":"cross-site"}).status_code == 403
    assert call(endpoint, {}, **{ca.HEADER:""}).status_code == 401


def test_create_limits_candidate_and_name(studio, monkeypatch):
    d, call, ids = studio
    url = BASE + "/comments"
    valid = operation(no="1", candidate_id=ids[0], body="字"*1000)
    for bad in [dict(body="字"*1001), dict(body=[]), dict(body=" "), dict(no=2), dict(candidate_id="absent"), dict(request_id=None)]:
        assert call(url, {**valid, **bad}).status_code == 409
    with pytest.raises(ValueError, match="表示名"):
        comments.change(d, d.parent.parent, "unregistered", valid)
    assert call(url, valid).status_code == 200
    monkeypatch.setattr(comments, "LIMIT", 1)
    assert call(url, {**valid,"request_id":uuid.uuid4().hex}).status_code == 409
    assert call(url, valid).status_code == 200
    assert comments.display_name(d.parent.parent, comments.LOCAL) == {"display_name":"ローカル"}


@pytest.mark.parametrize("broken", ["{", "null", "[]", '{"version":1,"revision":9,"comments":[{}],"requests":{}}'])
def test_corrupt_comments_never_overwritten(studio, broken):
    d, call, ids = studio
    path = d / "material_comments.json"
    path.write_text(broken, encoding="utf-8")
    assert call(BASE+"/comments").status_code == 409
    assert call(BASE+"/comments", operation(no=1, candidate_id=ids[0], body="確認")).status_code == 409
    assert path.read_text(encoding="utf-8") == broken


def test_concurrent_comments_and_adoption(studio):
    d, _, ids = studio
    lib = store.load_library(d)
    create = operation(no=1, candidate_id=ids[0], body="合成")
    with ThreadPoolExecutor(max_workers=5) as pool:
        posts = [pool.submit(comments.change, d, d.parent.parent, comments.LOCAL, create) for _ in range(4)]
        adoption = pool.submit(store.selection_change, d, {"selection_revision":lib["selection_revision"],
                               "action":"select","candidate_id":ids[1],"no":"1"})
        results = [task.result() for task in posts]
        adoption.result()
    assert results == [results[0]]*4 and comments.counts(d)["open"] == 1
    assert store.load_library(d)["selection_revision"] == lib["selection_revision"] + 1
    assert store.handoff(d)["ready_for_editing"] and store.handoff(d)["review_comments"]["open"] == 1


@pytest.mark.parametrize("mode", ["all","open","none"])
def test_csv_and_sheet_scope_jst_and_private_fields(studio, monkeypatch, mode):
    d, call, ids = studio
    monkeypatch.setattr(store, "now", lambda:"2026-11-03T16:00:00+00:00")
    url = BASE+"/comments"
    parent = call(url, operation(no=1, candidate_id=ids[0], body="=SUM(1,2)")).json["id"]
    call(url, operation(no=1, candidate_id=ids[0], parent_id=parent, body="<矢印>"), "artist")
    call(url+"/"+parent+"/status", operation(status="resolved"), "artist")
    call(url, operation(no=1, candidate_id=ids[1], body="確認中"))
    content = call(BASE+"/review.csv?comments="+mode).data.decode("utf-8-sig")
    headers, values = list(csv.reader(io.StringIO(content)))
    assert headers[2:4] == ["コメント","スタジオのコメント"] and values[2] == ""
    exported = values[3]
    assert ("[済] 確認係 11/4（前の候補へ）：=SUM" in exported) == (mode == "all")
    assert ("↳ [済] 作り手" in exported) == (mode == "all")
    assert ("[未対応] 確認係 11/4：確認中" in exported) == (mode != "none")
    assert "@" not in exported
    if mode == "all":
        assert call(BASE+"/review.csv").data.decode("utf-8-sig") == content
    response = call(BASE+"/review.xlsx?comments="+mode, {})
    workbook = load_workbook(io.BytesIO(response.data))
    sheet = workbook.active
    assert (sheet["D2"].value or "") == exported and sheet["D2"].data_type != "f"
    assert sheet["C2"].fill.fgColor.rgb.endswith("FFF5CD") and sheet["C2"].value in (None, "")
    workbook.close()
    assert review.csv_text("=1+2").startswith("'")
    assert call(BASE+"/review.csv?comments=invalid").status_code == 409


def test_comment_browser_model():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for browser contract tests")
    subprocess.run([node, "--test", "tests/test_material_comments.cjs"], check=True,
                   cwd=Path(__file__).resolve().parents[1])


def test_full_discussion_preserved_past_excel_cell_limit(studio, monkeypatch):
    d, _, _ = studio
    long_text = "円🪙" * 16000 + "\n終わり"
    monkeypatch.setattr(review, "export_lines", lambda *_: {"1":long_text})
    stream = io.BytesIO()
    review.write_workbook(d, stream)
    workbook = load_workbook(stream)
    assert "コメントの続き" in workbook.active["D2"].value
    rows = list(workbook["コメントの続き"].values)[1:]
    assert "".join(row[2] for row in rows) == long_text
    workbook.close()
