"""Offline integration checks for persistent adoption and channel isolation."""
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as appmod
import material_routes
import material_store as store
from pipeline import SentencePipeline


@pytest.fixture
def studio(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path / "output")
    appmod.OUTPUT_DIR.mkdir()
    monkeypatch.setattr(appmod, "APP_PASSWORD", "test-password")
    appmod.app.config.update(TESTING=True)
    material_routes.ACTIVE.clear()
    client = appmod.app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["material_csrf"] = "offline-token"
    return client, {"X-Material-CSRF": "offline-token"}, tmp_path


def make_job(root, name="sample", channel="economy"):
    d = root / "output" / name
    (d / "images").mkdir(parents=True)
    project = store.create_project(root, channel, "試験用の原稿", {
        "worldview_desc": "same flat drawing", "style_lock": True,
        "provider": "gpt-image", "openai_quality": "medium", "style_preset": "flat_infographic"
    })
    store.bind_job(root, d, project)
    store.save(d / "job.json", {"status": "completed", "channel_id": project["source_channel_id"]})
    store.save(d / "rows_progress.json", {"rows": [
        {"no": 1, "chapter_index": 1, "sentence": "価格と供給の関係を見ます。", "route": "diagram", "filename": "one.png", "display": "image"},
        {"no": 2, "chapter_index": 1, "sentence": "前の図を見ながら続けます。", "display": "hold"},
        {"no": 3, "chapter_index": 2, "sentence": "次の話です。", "display": "none"},
    ]})
    Image.new("RGB", (32, 18), "blue").save(d / "images" / "one.png")
    lib = store.load_library(d)
    store.sync_originals(d, lib)
    store.commit_library(d, lib, "sync")
    return d, project, lib["candidates"]["1"][0]


def choose(d, candidate):
    return store.selection_change(d, {"revision": store.load_library(d)["revision"], "no": 1,
                                      "action": "select", "candidate_id": candidate["id"]})


def test_channel_entry_templates_and_no_secret_output(studio, monkeypatch):
    client, _, root = studio
    monkeypatch.setenv("KEIZAI_OPENAI_API_KEY", "do-not-print-this")
    for channel in ("economy", "russia", "china", "success"):
        response = client.get("/materials?channel=" + channel)
        assert response.status_code == 200
        assert "素材スタジオ" in response.text
        assert "do-not-print-this" not in response.text
    assert "zukai-studio.onrender.com" in client.get("/materials?channel=russia").text
    assert client.get("/?channel_id=nonexistent").status_code == 400
    make_job(root)
    assert "/progress/sample" in client.get("/?channel_id=keizai").text
    assert "/progress/sample" not in client.get("/?channel_id=roshia").text


def test_auth_csrf_and_login_return(studio):
    client, headers, _ = studio
    assert client.post("/api/material-projects", json={}).status_code == 403
    client.get("/logout")
    assert client.get("/api/material-projects/x").status_code == 401
    assert client.post("/api/material-projects", json={}, headers=headers).status_code == 401
    response = client.get("/materials?channel=economy")
    assert response.status_code == 302
    response = client.post("/login", data={"password": "test-password"})
    assert response.headers["Location"] == "/materials?channel=economy"


def test_trello_identity_and_profile_freeze(studio):
    client, headers, root = studio
    body = {"channel_id": "economy", "title": "原稿A", "trello_url": "https://trello.com/c/Abcd1234/title"}
    one = client.post("/api/material-projects", json=body, headers=headers).get_json()["project"]
    two = client.post("/api/material-projects", json={**body, "title": "原稿B"}, headers=headers).get_json()["project"]
    assert one == two and one["project_id"] == "trello-Abcd1234"
    assert one["profile"]["coverage_mode"] == "scene"
    assert client.post("/api/material-projects", json={**body, "channel_id": "russia"}, headers=headers).status_code == 409
    config = {"style_lock": True}
    project = store.create_project(root, "success", "設定を保存", config)
    config["style_lock"] = False
    assert store.get_project(root, project["project_id"])["profile"]["style_lock"] is True
    assert client.get("/?project_id=" + one["project_id"]).status_code == 200
    direct = client.get("/materials?channel=economy&trello=https://trello.com/c/Abcd1234")
    assert direct.status_code == 302
    assert one["project_id"] in direct.headers["Location"]


@pytest.mark.parametrize("url", ["http://trello.com/c/Abcd1234", "https://trello.com.evil/c/Abcd1234", "https://trello.com/c/no"])
def test_bad_card_links(studio, url):
    client, headers, _ = studio
    assert client.post("/api/material-projects", json={"channel_id": "economy", "title": "x", "trello_url": url}, headers=headers).status_code == 409


def test_china_does_not_silently_spend_shared_keys(monkeypatch):
    for suffix in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(suffix, "shared")
        monkeypatch.delenv("CHINA_" + suffix, raising=False)
    assert not any(appmod.resolve_channel_keys(appmod.get_channel("china")).values())
    monkeypatch.setenv("CHINA_OPENAI_API_KEY", "dedicated")
    assert appmod.resolve_channel_keys(appmod.get_channel("china"))["openai"] == "dedicated"


def test_adopted_file_survives_legacy_regeneration(studio):
    client, headers, root = studio
    d, _, original = make_job(root)
    choose(d, original)
    Image.new("RGB", (32, 18), "red").save(d / "images" / "one.png")
    response = client.post("/api/material-jobs/sample/sync", headers=headers)
    assert response.status_code == 200
    lib = response.get_json()["library"]
    assert len(lib["candidates"]["1"]) == 2
    assert lib["selections"]["1"] == original["id"]
    assert store.digest(d / original["filename"]) == original["sha256"]
    assert client.get("/materials/jobs/sample").status_code == 200


def test_revision_and_cross_scene_validation(studio):
    client, headers, root = studio
    d, _, original = make_job(root)
    body = {"revision": 1, "no": 1, "action": "select", "candidate_id": original["id"]}
    assert client.post("/api/material-jobs/sample/selection", json=body, headers=headers).status_code == 200
    assert client.post("/api/material-jobs/sample/selection", json=body, headers=headers).status_code == 409
    assert client.post("/api/material-jobs/sample/selection", json={**body, "revision": 2, "no": 2}, headers=headers).status_code == 409
    before = store.load_library(d)
    with pytest.raises(ValueError):
        store.selection_change(d, {"revision": 2, "no": 1, "action": "select", "candidate_id": "wrong"})
    assert store.load_library(d) == before


def test_undo_and_flags_change_readiness(studio):
    _, _, root = studio
    d, _, original = make_job(root)
    choose(d, original)
    assert store.handoff(d)["ready_for_editing"]
    store.selection_change(d, {"revision": 2, "no": 1, "action": "flag", "note": "ラベルを確認"})
    assert not store.handoff(d)["ready_for_editing"]
    store.selection_change(d, {"revision": 3, "no": 1, "action": "undo"})
    assert store.handoff(d)["ready_for_editing"]


def test_hold_cannot_cross_chapter_or_blank(studio):
    _, _, root = studio
    d, _, original = make_job(root)
    choose(d, original)
    h = store.handoff(d)
    assert h["rows"][1]["hold_asset_id"] == original["id"]
    store.selection_change(d, {"revision": 2, "no": 3, "action": "hold"})
    assert not store.handoff(d)["ready_for_editing"]
    assert store.handoff(d)["rows"][2]["hold_asset_id"] is None


@pytest.mark.parametrize("path", ["../outside.png", "/absolute.png", "C:/file.png", "images\\one.png"])
def test_asset_path_validation(tmp_path, path):
    with pytest.raises(ValueError):
        store.safe_path(tmp_path, path)


def test_export_exact_adoption_and_no_video_approval(studio):
    client, _, root = studio
    d, _, original = make_job(root)
    choose(d, original)
    response = client.get("/materials/jobs/sample/download")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        payload = json.loads(archive.read("material-handoff.json"))
        assert payload["ready_for_editing"] is True
        assert payload["approval"]["video_approved"] is False
        assert payload["cost"]["amount"] is None
        assert payload["rows"][1]["timing_status"] == "narration_alignment_required"
        assert archive.read(original["filename"]) == (d / original["filename"]).read_bytes()
        assert len([p for p in archive.namelist() if p.startswith("material_assets/")]) == 1
    (d / original["filename"]).write_bytes(b"changed")
    assert client.get("/materials/jobs/sample/download").status_code == 409


def test_local_flip_and_duplicate_request_do_not_overwrite(studio):
    client, headers, root = studio
    d, _, original = make_job(root)
    # Different pixels make a real flipped candidate, not a duplicate hash.
    image = Image.new("RGB", (32, 18), "blue")
    image.putpixel((0, 0), (255, 0, 0))
    image.save(d / "images" / "one.png")
    client.post("/api/material-jobs/sample/sync", headers=headers)
    original = store.load_library(d)["candidates"]["1"][-1]
    choose(d, original)
    body = {"request_id": "flip-test", "no": "1", "kind": "flip",
            "candidate_id": original["id"], "revision": str(store.load_library(d)["revision"])}
    response = client.post("/api/material-jobs/sample/variant", data=body, headers=headers)
    assert response.status_code == 200, response.text
    revision = response.get_json()["library"]["revision"]
    assert client.post("/api/material-jobs/sample/variant", data=body, headers=headers).status_code == 200
    lib = store.load_library(d)
    assert lib["revision"] == revision
    assert lib["selections"]["1"] == original["id"]
    assert lib["requests"]["flip-test"]["requested_images"] == 0


def test_generate_uses_frozen_style_and_keeps_selection(studio, monkeypatch):
    client, headers, root = studio
    d, _, original = make_job(root)
    choose(d, original)
    monkeypatch.setenv("KEIZAI_OPENAI_API_KEY", "offline-key")
    calls = []
    def fake_generation(**kwargs):
        calls.append(kwargs)
        Image.new("RGB", (32, 18), "green").save(kwargs["output_dir"] / "two.png")
        return [{"success": True, "filename": "two.png"}]
    monkeypatch.setattr("generator.run_parallel_generation", fake_generation)
    body = {"request_id": "generate-test", "no": "1", "kind": "edit", "candidate_id": original["id"],
            "instruction": "図を大きく", "count": "1", "revision": "2"}
    response = client.post("/api/material-jobs/sample/variant", data=body, headers=headers)
    assert response.status_code == 200, response.text
    client.post("/api/material-jobs/sample/variant", data=body, headers=headers)
    assert len(calls) == 1
    assert calls[0]["style_lock_text"] == "same flat drawing"
    assert calls[0]["edit_image_path"] == str((d / original["filename"]).resolve())
    assert store.load_library(d)["selections"]["1"] == original["id"]


def test_generation_failure_preserves_and_never_auto_retries(studio, monkeypatch):
    client, headers, root = studio
    d, _, original = make_job(root)
    choose(d, original)
    monkeypatch.setenv("KEIZAI_OPENAI_API_KEY", "offline-key")
    calls = []
    def fail(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("provider secret details")
    monkeypatch.setattr("generator.run_parallel_generation", fail)
    body = {"request_id": "failed-test", "no": "1", "kind": "generate", "revision": "2"}
    response = client.post("/api/material-jobs/sample/variant", data=body, headers=headers)
    assert response.status_code == 409
    assert "secret" not in response.text
    client.post("/api/material-jobs/sample/variant", data=body, headers=headers)
    assert len(calls) == 1
    assert store.load_library(d)["selections"]["1"] == original["id"]
    assert not material_routes.ACTIVE


def test_start_binds_project_and_freezes_effective_settings(studio, monkeypatch):
    client, _, root = studio
    project = store.create_project(root, "economy", "引き継ぐ案件", {"provider": "gpt-image", "style_lock": True})
    monkeypatch.setenv("KEIZAI_OPENAI_API_KEY", "offline-key")
    launched = []
    class FakeThread:
        def __init__(self, **kwargs): launched.append(kwargs)
        def start(self): pass
    monkeypatch.setattr(appmod.threading, "Thread", FakeThread)
    body = {"channel_id": "keizai", "project_id": project["project_id"], "provider": "gpt-image",
            "manuscript_text": "試験用の原稿です。" * 20, "worldview_mode": "on", "worldview_desc": "saved drawing"}
    response = client.post("/start", data=body)
    assert response.status_code == 200, response.text
    job_id = response.get_json()["job_id"]
    context = store.read(root / "output" / job_id / "material_context.json")
    assert context["project_id"] == project["project_id"]
    assert context["generation_settings"]["worldview_desc"] == "saved drawing"
    assert launched[0]["kwargs"]["profile_override"] == project["profile"]
    assert client.post("/start", data={**body, "channel_id": "roshia"}).status_code == 409


def test_new_studio_does_not_force_hold_scenes_into_images():
    pipeline = object.__new__(SentencePipeline)
    pipeline.coverage_mode = "scene"
    pipeline.beat_mode = True
    assert pipeline._force_high_coverage_images([{"no": 1, "display": "hold"}], []) == 0
