"""Ordered images in a single Russian narration sentence, from selection to editor."""
import csv
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from openpyxl import load_workbook
from PIL import Image

import material_review as review
import material_comments as comments
import material_store as store
from test_materials import make_job, studio


def scene(root, channel="russia"):
    job, _, _ = make_job(root, channel=channel)
    rows = store.source_rows(job)
    for number, row in enumerate(rows, 12):
        row["no"] = number
    store.save(job / "rows_progress.json", {"rows": rows})
    library = store.load_library(job)
    store.sync_originals(job, library)
    images = list(library["candidates"]["12"])
    for filename, colour, route in [("second.jpg", "red", "realphoto"), ("third.png", "green", "diagram")]:
        path = job / "images" / filename
        Image.new("RGB", (20, 10), colour).save(path)
        images.append(store.snapshot(job, library, 12, path, filename, {"route": route, "web_source_url": "https://example.org/" + filename}))
    library["selections"]["12"] = images[0]["id"]
    library.pop("additional_selections", None)  # On disk before the multiple-adoption release.
    store.commit_library(job, library, "fixture")
    return job, images


def change(job, action, cid=None, **kw):
    return store.selection_change(job, {"selection_revision": store.load_library(job)["selection_revision"],
                                        "no": 12, "action": action, "candidate_id": cid, **kw})


def test_old_selection_append_move_remove_undo_and_continuation(studio):
    _, _, root = studio
    job, images = scene(root)
    a, b, c = [image["id"] for image in images]
    original_hash = store.selection_hash(job)
    assert store.selected_ids(store.load_library(job), 12) == [a]
    old_revision = store.load_library(job)["selection_revision"]
    added = change(job, "append", b)
    assert change(job, "append", b)["selection_revision"] == added["selection_revision"]
    with pytest.raises(ValueError, match="更新"):
        change(job, "append", c, selection_revision=old_revision)
    with pytest.raises(ValueError, match="採用中"):
        change(job, "hide", b)
    change(job, "append", c)
    assert store.selected_ids(change(job, "earlier", c), 12) == [a, c, b]
    assert store.handoff(job)["rows"][1]["hold_asset_id"] == b
    assert store.selected_ids(change(job, "later", c), 12) == [a, b, c]
    same_hash = store.selection_hash(job)
    change(job, "clear", a)
    assert store.selected_ids(store.load_library(job), 12) == [b, c]
    assert store.selected_ids(change(job, "undo"), 12) == [a, b, c]
    assert store.selection_hash(job) == same_hash
    change(job, "none")
    assert store.selected_ids(store.load_library(job), 12) == []
    change(job, "undo")
    assert store.selected_ids(change(job, "clear", c), 12) == [a, b]
    change(job, "clear", b)
    assert store.selection_hash(job) == original_hash
    change(job, "append", b)
    assert store.selected_ids(change(job, "select", c), 12) == [c, b]


def test_zip_sheet_and_csv_have_the_same_order_notes_and_actual_bytes(studio):
    client, headers, root = studio
    job, images = scene(root)
    a, b, c = images
    change(job, "append", c["id"])
    change(job, "append", b["id"])
    change(job, "earlier", b["id"])
    initial_hash = store.selection_hash(job)
    note = "「ところが」からこの画像"
    store.edit_note_change(job, {"no": 12, "candidate_id": b["id"], "note": note})
    assert store.selection_hash(job) != initial_hash
    api = "/api/material-jobs/" + job.name
    live = client.get(api + "/handoff").json
    assert len(live["rows"]) == 3 and live["ready_for_editing"]
    assert live["rows"][0]["asset"]["filename"] == a["filename"]
    assert live["rows"][1]["hold_asset_id"] == c["id"]
    files = ["images/12.png", "images/12-2.jpg", "images/12-3.png"]
    ids = [a["id"], b["id"], c["id"]]
    numbers = ["12", "12-2", "12-3"]
    response = client.post(api + "/export", headers=headers,
                           json={"revision": live["material_revision"], "selection_hash": live["selection_hash"]})
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        snapshot = json.loads(archive.read("material-handoff.json"))
        first = snapshot["rows"][0]
        assert first["asset"] == first["image_sequence"][0]["asset"]
        assert [item["asset"]["filename"] for item in first["image_sequence"]] == files
        assert [item["asset"]["id"] for item in first["image_sequence"]] == ids
        assert [item["edit_note"] for item in first["image_sequence"]] == ["", note, ""]
        assert [str(item["number"]) for item in first["image_sequence"]] == numbers
        assert sorted(n for n in archive.namelist() if Path(n).suffix in {".png", ".jpg"}) == sorted(files)
        for name, image in zip(files, images):
            assert hashlib.sha256(archive.read(name)).hexdigest() == image["sha256"]
        table = list(csv.DictReader(io.StringIO(archive.read("採用素材.csv").decode("utf-8-sig"))))
        assert [r["採用画像"] for r in table[:3]] == files
        assert [r["番号"] for r in table[:3]] == numbers
        assert [r["編集への指示"] for r in table[:3]] == ["", note, ""]
    csv_rows = list(csv.DictReader(io.StringIO(client.get(api + "/review.csv").data.decode("utf-8-sig"))))
    assert [r["番号"] for r in csv_rows] == numbers + ["13", "14"]
    assert [r["編集への指示"] for r in csv_rows] == ["", note, "", "", ""]
    assert [r["画像URL"].rsplit("/", 1)[-1] for r in csv_rows[:4]] == ids + [c["id"]]
    response = client.post(api + "/review.xlsx", headers=headers)
    assert response.headers["X-Review-Rows"] == "5"
    workbook = load_workbook(io.BytesIO(response.data))
    sheet = workbook.active
    assert [sheet.cell(i, 1).value for i in range(2, 5)] == numbers
    assert sheet["A3"].data_type == "s" and sheet["A3"].number_format == "@"
    assert sheet["F3"].value == note and sheet["F4"].value in (None, "")
    assert len(sheet._images) == 4
    workbook.close()
    raw = "№,画像\n12,one.png\n"
    for expand, expected in [(True, numbers), (False, ["12"])]:
        content = review.extend_legacy_csv(job, raw, expand_adoptions=expand)
        assert [r["№"] for r in csv.DictReader(io.StringIO(content.decode("utf-8-sig")))] == expected
    assert not client.get(api).json["export"]["changed"]
    change(job, "earlier", c["id"])
    assert client.get(api).json["export"]["changed"]
    (job / b["filename"]).write_bytes(b"corrupt")
    assert client.get(api + "/handoff").status_code == 409


def test_other_channels_and_hidden_or_foreign_candidates(studio):
    client, headers, root = studio
    job, images = scene(root, "economy")
    assert 'id="review-sequence"' not in client.get("/materials/jobs/" + job.name).text
    endpoint = "/api/material-jobs/" + job.name + "/selection"
    body = {"no": 12, "action": "append", "candidate_id": images[1]["id"],
            "selection_revision": store.load_library(job)["selection_revision"]}
    assert client.post(endpoint, headers=headers, json=body).status_code == 409
    assert store.selected_ids(change(job, "select", images[1]["id"]), 12) == [images[1]["id"]]
    other_path = root / "russia-fixture"
    job, images = scene(other_path)
    change(job, "hide", images[1]["id"])
    with pytest.raises(ValueError, match="非表示"):
        change(job, "append", images[1]["id"])
    with pytest.raises(ValueError, match="候補"):
        change(job, "append", store.load_library(job)["candidates"]["1"][0]["id"])


def test_old_russian_channel_id_and_image_specific_discussion(studio):
    _, _, root = studio
    job, images = scene(root)
    context = store.read(job / "material_context.json")
    context.pop("channel_id")
    store.save(job / "material_context.json", context)
    change(job, "append", images[1]["id"])
    assert store.handoff(job)["rows"][0]["image_sequence"][1]["asset"]["archive_filename"] == "images/12-2.jpg"
    for i, image in enumerate(images):
        comments.change(job, root, comments.LOCAL, {
            "no": 12, "candidate_id": image["id"], "body": f"指摘{i}",
            "request_id": f"sequence-comment-{i}"})
    values = [r["values"] for r in review.review_rows(job)[1]]
    column = review.HEADERS.index("スタジオのコメント")
    assert "指摘0" in values[0][column] and "指摘2" in values[0][column]
    assert "指摘1" not in values[0][column] and "指摘1" in values[1][column]
    assert "前の候補へ" not in values[1][column]
