"""Image-specific and image-free editor directions through the studio and its exports."""
import csv
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as appmod
import material_review as review
import material_store as store

URL = "/api/material-jobs/sample/edit-note"
HEADERS = {"X-Material-CSRF": "test-csrf"}


@pytest.fixture
def sample(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(appmod, "APP_PASSWORD", "test-password")
    d = tmp_path / "sample"
    (d / "images").mkdir(parents=True)
    Image.new("RGB", (640, 360), "red").save(d / "images" / "1.png")
    Image.new("RGB", (640, 360), "green").save(d / "images" / "1b.png")
    Image.new("RGB", (640, 360), "blue").save(d / "images" / "3.png")
    rows = [
        {"no": 1, "chapter_index": 1, "sentence": "値段は同じでも中身が減ります。", "route": "diagram",
         "display": "image", "status": "ok", "filename": "1.png"},
        {"no": 2, "chapter_index": 1, "sentence": "前の図を続けます。", "route": "diagram", "display": "hold", "status": "hold"},
        {"no": 3, "chapter_index": 1, "sentence": "まだ選んでいない場面です。", "route": "illustration",
         "display": "image", "status": "ok", "filename": "3.png"},
    ]
    store.save(d / "rows_progress.json", {"rows": rows})
    store.save(d / "job.json", {"status": "completed", "channel_id": "keizai"})
    store.save(d / "manifest.json", {"title": "編集への指示の確認", "rows": []})
    store.save(d / "material_context.json", {"title": "編集への指示の確認", "channel_id": "keizai", "profile": {}})
    library = store.load_library(d)
    first = store.snapshot(d, library, 1, d / "images" / "1.png", "最初の候補")
    second = store.snapshot(d, library, 1, d / "images" / "1b.png", "別の案")
    third = store.snapshot(d, library, 3, d / "images" / "3.png", "最初の候補")
    library["selections"]["1"] = first["id"]
    library["display"]["2"] = "hold"
    store.commit_library(d, library, "fixture")
    client = appmod.app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["material_csrf"] = "test-csrf"
    return d, client, first["id"], second["id"], third["id"]


def save(client, no, candidate_id, note):
    return client.post(URL, data=json.dumps({"no": no, "candidate_id": candidate_id, "note": note}),
                       content_type="application/json", headers=HEADERS)


def save_scene(client, no, note):
    return client.post(URL, json={"scope": "scene", "no": no, "note": note}, headers=HEADERS)


def choose(client, no, action, candidate_id=None):
    api = "/api/material-jobs/sample"
    revision = client.get(api).get_json()["library"]["selection_revision"]
    response = client.post(api + "/selection", headers=HEADERS, json={
        "selection_revision": revision, "no": no, "action": action, "candidate_id": candidate_id})
    assert response.status_code == 200


def test_scene_directions_reach_sheet_csv_and_editor_zip_without_an_image(sample):
    d, client, _, _, third = sample
    note = "「店頭で」からFLOW動画「店頭.mp4」を使用\nhttps://example.com/flow"
    save(client, 3, third, "この候補を採用する時の指示")
    assert save_scene(client, 3, note).status_code == 200
    pending = store.handoff(d)
    assert pending["rows"][2]["edit_note"] == note
    assert pending["rows"][2]["asset"] is None and not pending["ready_for_editing"]
    assert review.review_rows(d)[1][2]["values"][review.HEADERS.index("編集への指示")] == note

    choose(client, 3, "none")
    handoff = client.get("/api/material-jobs/sample/handoff").get_json()
    assert handoff["ready_for_editing"] and handoff["rows"][2]["asset"] is None
    response = client.post("/api/material-jobs/sample/export", headers=HEADERS,
                           json={"revision": handoff["material_revision"], "selection_hash": handoff["selection_hash"]})
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        row = json.loads(archive.read("material-handoff.json"))["rows"][2]
        assert (row["display"], row["asset"], row["edit_note"]) == ("none", None, note)
        table = list(csv.DictReader(io.StringIO(archive.read("採用素材.csv").decode("utf-8-sig"))))
        assert table[2]["採用画像"] == "" and table[2]["編集への指示"] == note
        assert store.candidate(store.load_library(d), 3, third)["filename"] not in archive.namelist()
    csv_rows = list(csv.DictReader(io.StringIO(client.get("/api/material-jobs/sample/review.csv").data.decode("utf-8-sig"))))
    assert csv_rows[2]["編集への指示"] == note and csv_rows[2]["画像URL"] == ""
    workbook = io.BytesIO()
    review.write_workbook(d, workbook)
    sheet = load_workbook(workbook).active
    assert sheet["F4"].value == note and sheet["K4"].value in (None, "")
    assert all(image.anchor._from.row != 3 for image in sheet._images)
    assert not client.get("/api/material-jobs/sample").get_json()["export"]["changed"]
    assert save_scene(client, 3, "別の共有URLに変更").status_code == 200
    assert client.get("/api/material-jobs/sample").get_json()["export"]["changed"]


def test_scene_with_zero_candidates_and_legacy_library(sample):
    d, client, *_ = sample
    path = d / "material_library.json"
    old = store.read(path)
    old.pop("scene_edit_notes", None)
    store.save(path, old)
    original = store.selection_hash(d)
    assert "scene_edit_notes" not in store.read(path)
    note = "字幕を重ねる"
    data = save_scene(client, 2, "  " + note + "  ").get_json()["library"]
    assert data["scene_edit_notes"] == {"2": note} and data["selection_revision"] == old["selection_revision"]
    assert not data["candidates"].get("2")
    assert save_scene(client, 2, note).get_json()["library"]["revision"] == data["revision"]
    row = store.handoff(d)["rows"][1]
    assert row["edit_note"] == note and row["display"] == "hold" and row["hold_asset_id"]
    assert row["asset"] is None and store.selection_hash(d) != original
    choose(client, 2, "none")
    assert store.handoff(d)["rows"][1]["edit_note"] == note
    choose(client, 2, "undo")
    assert save_scene(client, 2, "").get_json()["library"]["scene_edit_notes"] == {}
    assert store.selection_hash(d) == original


def test_changing_adoption_restores_the_matching_note(sample):
    d, client, _, _, third = sample
    save_scene(client, 3, "FLOW動画")
    save(client, 3, third, "画像をズーム")
    choose(client, 3, "select", third)
    selected = store.selection_hash(d)
    save_scene(client, 3, "FLOW動画の共有先を変更")
    assert store.selection_hash(d) == selected
    assert store.handoff(d)["rows"][2]["edit_note"] == "画像をズーム"
    assert review.review_rows(d)[1][2]["values"][review.HEADERS.index("編集への指示")] == "画像をズーム"
    choose(client, 3, "clear", third)
    assert store.handoff(d)["rows"][2]["edit_note"] == "FLOW動画の共有先を変更"
    choose(client, 3, "undo")
    assert store.handoff(d)["rows"][2]["edit_note"] == "画像をズーム"


def test_scene_note_validates_the_target_and_length_before_saving(sample):
    d, client, first, *_ = sample
    assert save_scene(client, 8, "無い場面").status_code == 409
    assert save_scene(client, 3, "あ" * (store.EDIT_NOTE_MAX + 1)).status_code == 409
    assert client.post(URL, json={"scope": "scene", "no": 3, "note": "x"}).status_code == 403
    for invalid in ({"scope": "scene", "candidate_id": first}, {"scope": "unknown"}, {}):
        assert client.post(URL, json={"no": 3, "note": "x", **invalid}, headers=HEADERS).status_code == 409
    assert store.load_library(d)["scene_edit_notes"] == {}


def test_note_is_saved_per_image_and_can_be_cleared(sample):
    d, client, first, second, _ = sample
    before = store.load_library(d)
    response = save(client, 1, first, "  ×印を入れる\r\n出典：https://example.com/photo  ")
    assert response.status_code == 200
    lib = store.load_library(d)
    assert lib["edit_notes"] == {first: "×印を入れる\n出典：https://example.com/photo"}
    assert response.get_json()["library"]["edit_notes"][first].startswith("×印")
    assert lib["history"][-1]["action"] == "edit_note" and lib["history"][-1]["candidate_id"] == first
    # 採用・非表示の「戻す」の対象ではない。指示を書いても直前の採用操作は戻せる。
    assert lib["selection_revision"] == before["selection_revision"]
    assert lib["selections"] == before["selections"]
    # 同じ場面の別の案には別の指示。採用を替えても指示は画像に付いたまま。
    assert save(client, 1, second, "彩度を少しずつ落とす").status_code == 200
    assert store.load_library(d)["edit_notes"][second] == "彩度を少しずつ落とす"
    revision = store.load_library(d)["revision"]
    assert save(client, 1, second, "彩度を少しずつ落とす").status_code == 200
    assert store.load_library(d)["revision"] == revision  # 変わらない保存は記録を増やさない
    assert save(client, 1, first, "").status_code == 200
    assert first not in store.load_library(d)["edit_notes"]


def test_note_rejects_wrong_targets_long_text_and_missing_csrf(sample):
    d, client, first, _, third = sample
    assert save(client, 3, first, "別の場面の画像").status_code == 409
    assert save(client, 9, first, "無い場面").status_code == 409
    assert save(client, 1, "1-unknown", "無い画像").status_code == 409
    too_long = save(client, 1, first, "あ" * (store.EDIT_NOTE_MAX + 1))
    assert too_long.status_code == 409 and "1000字" in too_long.get_json()["error"]
    assert save(client, 1, first, "あ" * store.EDIT_NOTE_MAX).status_code == 200
    no_csrf = client.post(URL, data=json.dumps({"no": 1, "candidate_id": third, "note": "x"}),
                          content_type="application/json")
    assert no_csrf.status_code == 403
    assert third not in store.load_library(d)["edit_notes"]


def test_review_sheet_shows_the_note_of_the_image_in_each_row(sample):
    d, client, first, second, third = sample
    save(client, 1, first, "×印を入れる")
    save(client, 1, second, "使っていない案への指示")
    save(client, 3, third, "出典：https://example.com/a")
    _, rows = review.review_rows(d)
    column = review.HEADERS.index("編集への指示")
    assert review.HEADERS[column - 1] == "原稿"
    assert [r["values"][column] for r in rows] == ["×印を入れる", "", "出典：https://example.com/a"]
    stream = io.BytesIO()
    review.write_workbook(d, stream)
    sheet = load_workbook(stream).active
    assert sheet["F1"].value == "編集への指示" and sheet["F2"].value == "×印を入れる"
    assert sheet["F2"].fill.fgColor.rgb.endswith("E8F1FB")
    assert sheet["F3"].value in (None, "")  # 前の素材を継続する行には重ねて書かない
    content = client.get("/api/material-jobs/sample/review.csv").data.decode("utf-8-sig")
    table = list(csv.reader(io.StringIO(content)))
    assert table[0][column] == "編集への指示" and table[1][column] == "×印を入れる"


def test_editor_handoff_carries_only_the_adopted_images_note(sample):
    d, client, first, second, third = sample
    save(client, 1, second, "使っていない案への指示")
    save(client, 3, third, "未採用の場面の指示")
    data = store.handoff(d)
    assert [row["edit_note"] for row in data["rows"]] == ["", "", ""]
    save(client, 1, first, "×印を入れる\n出典：https://example.com/photo")
    data = store.handoff(d)
    assert data["schema_version"] == 1
    assert [row["edit_note"] for row in data["rows"]] == ["×印を入れる\n出典：https://example.com/photo", "", ""]
    response = client.get("/materials/jobs/sample/download")
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        handoff = json.loads(archive.read("material-handoff.json"))
        table = list(csv.reader(io.StringIO(archive.read("採用素材.csv").decode("utf-8-sig"))))
    assert handoff["rows"][0]["edit_note"].startswith("×印を入れる")
    column = table[0].index("編集への指示")
    assert table[0][column - 1] == "採用画像"
    assert table[1][column] == "×印を入れる\n出典：https://example.com/photo"
    assert table[1][table[0].index("要修正メモ")] == ""


def test_export_identity_changes_only_when_an_adopted_images_note_changes(sample):
    d, client, first, second, _ = sample
    before = store.selection_hash(d)
    save(client, 1, second, "使っていない案への指示")
    assert store.selection_hash(d) == before  # 編集担当に届かない指示では「変更あり」にしない
    save(client, 1, first, "×印を入れる")
    changed = store.selection_hash(d)
    assert changed != before
    save(client, 1, first, "")
    assert store.selection_hash(d) == before  # 指示の無い制作は、これまでと同じ照合値


def test_library_page_writes_notes_in_place_without_a_dialog(sample):
    _, client, *_ = sample
    page = client.get("/materials/jobs/sample").get_data(as_text=True)
    # 比較の窓の画像の下に、そのまま書ける欄（保存ボタンも説明の窓も無い）
    assert 'id="review-note-input"' in page and 'class="edit-note-input"' in page and 'maxlength="1000"' in page
    assert 'id="note-dialog"' not in page
    script = (Path(__file__).resolve().parents[1] / "static" / "material_library.js").read_text(encoding="utf-8")
    assert "/edit-note" in script and "edit-note-input" in script and "keepalive" in script
