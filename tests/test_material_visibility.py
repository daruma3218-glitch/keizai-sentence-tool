"""Candidate hiding is reversible and never deletes images, comments or adoption."""
import io
import uuid
import zipfile

import pytest
from PIL import Image

import material_review
import material_comments
import material_store as store
from test_materials import studio, make_job, choose


def change(client, headers, d, action, item, **extra):
    lib = store.load_library(d)
    return client.post("/api/material-jobs/" + d.name + "/selection", headers=headers,
                       json={"selection_revision": lib["selection_revision"], "no": 1,
                             "action": action, "candidate_id": item["id"], **extra})


def test_hide_restore_undo_and_sync_keep_files_and_comments(studio):
    client, headers, root = studio
    d, _, item = make_job(root)
    material_comments.display_name(root, "fixture", {"request_id": uuid.uuid4().hex, "display_name": "確認係"})
    material_comments.change(d, root, "fixture", {"request_id": uuid.uuid4().hex,
                             "no": 1, "candidate_id": item["id"], "body": "この図を再確認"})
    protected = {p: p.read_bytes() for p in d.rglob("*") if p.is_file()}
    response = change(client, headers, d, "hide", item)
    assert response.status_code == 200
    lib = response.get_json()["library"]
    assert lib["hidden_candidates"] == {item["id"]: True}
    assert store.visible_candidates(lib, 1) == []
    assert lib["selections"] == {}
    assert lib["history"][-1]["candidate_id"] == item["id"]
    for p, original in protected.items():
        if p.name != "material_library.json":
            assert p.read_bytes() == original
    assert client.get("/materials/asset/sample/" + item["id"]).status_code == 200
    assert client.post("/api/material-jobs/sample/sync", headers=headers).status_code == 200
    assert store.visible_candidates(store.load_library(d), 1) == []
    assert change(client, headers, d, "select", item).status_code == 409
    assert change(client, headers, d, "restore", item).status_code == 200
    assert store.visible_candidates(store.load_library(d), 1) == [item]
    assert material_comments.listing(d, "fixture")["comments"][0]["body"] == "この図を再確認"
    assert change(client, headers, d, "undo", item).status_code == 200
    assert store.visible_candidates(store.load_library(d), 1) == []
    assert change(client, headers, d, "undo", item).status_code == 200
    assert store.visible_candidates(store.load_library(d), 1) == [item]


def test_adopted_candidate_cannot_be_hidden_and_conflicts_do_not_overwrite(studio):
    client, headers, root = studio
    d, _, item = make_job(root)
    stale = store.load_library(d)["selection_revision"]
    choose(d, item)
    before = (d / "material_library.json").read_bytes()
    response = change(client, headers, d, "hide", item)
    assert response.status_code == 409 and "採用中" in response.get_json()["error"]
    assert (d / "material_library.json").read_bytes() == before
    assert change(client, headers, d, "clear", item).status_code == 200
    assert change(client, headers, d, "hide", item).status_code == 200
    before = (d / "material_library.json").read_bytes()
    assert change(client, headers, d, "restore", item, selection_revision=stale).status_code == 409
    assert (d / "material_library.json").read_bytes() == before
    assert store.load_library(d)["selections"] == {}


@pytest.mark.parametrize("action", ["hide", "restore"])
def test_visibility_checks_login_csrf_scene_and_candidate(studio, action):
    client, headers, root = studio
    d, _, item = make_job(root)
    before = (d / "material_library.json").read_bytes()
    assert change(client, {}, d, action, item).status_code == 403
    assert change(client, headers, d, action, item, no=2).status_code == 409
    assert change(client, headers, d, action, {"id": "missing"}).status_code == 409
    with client.session_transaction() as session:
        session.pop("authenticated", None)
    assert change(client, headers, d, action, item).status_code == 401
    assert (d / "material_library.json").read_bytes() == before


def test_hidden_candidates_do_not_enter_review_exports_or_change_adopted_zip(studio):
    client, headers, root = studio
    d, _, item = make_job(root)
    # Add a different candidate, adopt it, then hide the original.
    path = d / "images" / "alternative.png"
    Image.new("RGB", (32, 18), "red").save(path)
    lib = store.load_library(d)
    alternative = store.snapshot(d, lib, 1, path, "別の候補")
    store.commit_library(d, lib, "fixture")
    choose(d, alternative)
    old_hash = store.selection_hash(d)
    assert change(client, headers, d, "hide", item).status_code == 200
    assert store.selection_hash(d) == old_hash
    _, rows = material_review.review_rows(d)
    assert rows[0]["sha256"] == alternative["sha256"]
    response = client.get("/materials/jobs/sample/download")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        images = [n for n in archive.namelist() if n.endswith(".png")]
        assert len(images) == 1
        assert archive.read(images[0]) == path.read_bytes()
    # If every candidate is hidden, review export must not fall back to images/one.png.
    assert change(client, headers, d, "clear", alternative).status_code == 200
    assert change(client, headers, d, "hide", alternative).status_code == 200
    _, rows = material_review.review_rows(d)
    for row in rows[:2]:
        assert row["path"] is None
        assert row["values"][material_review.HEADERS.index("画像URL")] == ""
    assert "すべて非表示" in rows[0]["values"][material_review.HEADERS.index("状態")]
    assert not store.handoff(d)["ready_for_editing"]
    # The newer hidden candidate cannot override the remaining visible candidate.
    assert change(client, headers, d, "restore", item).status_code == 200
    _, rows = material_review.review_rows(d)
    assert rows[0]["sha256"] == item["sha256"]


def test_legacy_library_and_previous_operation_migrate_without_losing_new_candidates(studio):
    client, headers, root = studio
    d, _, item = make_job(root)
    choose(d, item)
    previous = store.read(d / "material_previous.json")
    previous.pop("hidden_candidates")
    store.save(d / "material_previous.json", previous)
    lib = store.load_library(d)
    lib.pop("hidden_candidates")
    store.save(d / "material_library.json", lib)
    assert change(client, headers, d, "undo", item).status_code == 200
    assert store.load_library(d)["hidden_candidates"] == {}
    assert change(client, headers, d, "hide", item).status_code == 200
    lib = store.load_library(d)
    Image.new("RGB", (32, 18), "green").save(d / "images" / "new.png")
    new = store.snapshot(d, lib, 1, d / "images" / "new.png", "追加の候補")
    store.commit_library(d, lib, "operation_completed")
    assert change(client, headers, d, "undo", item).status_code == 200
    assert [c["id"] for c in store.visible_candidates(store.load_library(d), 1)] == [item["id"], new["id"]]
