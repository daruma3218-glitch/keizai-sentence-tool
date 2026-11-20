"""編集への指示：画像1枚ごとに、編集担当への指示と出典URLを1つの欄に残す（2026-11-20）。

コメント（状態つきの話し合い）とは別に残り、採用した画像の指示だけが
スプシ・CSV・編集担当へ渡すZIPに出る。指示は場面ではなく画像に付く。
"""
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


def test_library_page_has_note_dialog_and_review_button(sample):
    _, client, *_ = sample
    page = client.get("/materials/jobs/sample").get_data(as_text=True)
    assert 'id="note-dialog"' in page and 'id="edit-note"' in page and 'maxlength="1000"' in page
    assert 'id="review-note"' in page and 'id="review-note-text"' in page
    script = (Path(__file__).resolve().parents[1] / "static" / "material_library.js").read_text(encoding="utf-8")
    assert "/edit-note" in script and "data-action=\"note\"" in script
