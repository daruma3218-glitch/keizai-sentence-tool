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
    assert client.get("/api/material-generation-board").status_code == 401
    assert client.post("/api/material-projects", json={}, headers=headers).status_code == 401
    response = client.get("/materials?channel=economy")
    assert response.status_code == 302
    response = client.post("/login", data={"password": "test-password"})
    assert response.headers["Location"] == "/materials?channel=economy"


def test_live_board_is_global_fifo_read_only_and_excludes_private_settings(studio, monkeypatch):
    client, _, root = studio
    monkeypatch.setattr(appmod, "_RUNNING", {"job_id": "other-channel"})
    monkeypatch.setattr(appmod, "_QUEUE", ["queued-first", "queued-second"])
    states = {
        "other-channel": {"channel_id": "roshia", "title": "他のチャンネル", "status": "running", "percent": 34},
        "queued-first": {"channel_id": "keizai", "title_override": "入力した題", "status": "queued"},
        "queued-second": {"channel_id": "seikou", "title": "次の案件", "status": "queued"},
    }
    for state in states.values():
        state.update(openai_api_key="not-for-the-browser", manuscript_text="private-full-script")
    monkeypatch.setattr(appmod, "_get_job_state", lambda jid: states[jid])
    before = sorted(str(p) for p in root.rglob("*"))
    response = client.get("/api/material-generation-board")
    board = response.get_json()
    assert response.headers["Cache-Control"] == "no-store"
    assert board["running"]["id"] == "other-channel"
    assert [j["id"] for j in board["waiting"]] == ["queued-first", "queued-second"]
    assert board["waiting"][0]["title"] == "入力した題"
    for path in ("/materials", "/materials?channel=economy"):
        assert "他のチャンネル" in client.get(path).text
        assert "順番待ち" in client.get(path).text
    assert "not-for-the-browser" not in response.text and "private-full-script" not in response.text
    assert sorted(str(p) for p in root.rglob("*")) == before
    monkeypatch.setattr(appmod, "_RUNNING", {"job_id": None})
    monkeypatch.setattr(appmod, "_QUEUE", [])
    assert client.get("/api/material-generation-board").get_json() == {"running": None, "waiting": [], "accepting": True, "other_operations": 0}


def test_scene_fix_entry_preserves_chosen_channel(studio):
    client, _, _ = studio
    page = client.get("/scene-fix?channel_id=keizai")
    assert '<option value="keizai" selected>' in page.text
    assert client.get("/scene-fix?channel_id=unknown").status_code == 400


def test_manual_revision_flag_survives_adoption_and_export_records_content(studio):
    client, headers, root = studio
    d, _, candidate = make_job(root)
    lib = store.selection_change(d, {"revision":store.load_library(d)["revision"], "no":1, "action":"flag", "note":"文字を確認"})
    lib = store.selection_change(d, {"revision":lib["revision"], "no":1, "action":"select", "candidate_id":candidate["id"]})
    assert lib["flags"]["1"] == "文字を確認"
    plan = client.get("/api/material-jobs/sample/handoff").get_json()
    assert not plan["ready_for_editing"] and plan["missing"][0]["reason"] == "文字を確認"
    expected = {"revision":plan["material_revision"], "selection_hash":plan["selection_hash"]}
    assert client.post("/api/material-jobs/sample/export", json=expected).status_code == 403
    response = client.post("/api/material-jobs/sample/export", json=expected, headers=headers)
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        exported = json.loads(archive.read("material-handoff.json"))
        assert exported["selection_hash"] == plan["selection_hash"]
        assert not exported["ready_for_editing"]
    receipt = client.get("/api/material-jobs/sample").get_json()["export"]
    assert receipt["last"] and not receipt["changed"]
    store.selection_change(d, {"revision":lib["revision"], "no":1, "action":"unflag"})
    assert client.get("/api/material-jobs/sample").get_json()["export"]["changed"]
    assert client.post("/api/material-jobs/sample/export", json=expected, headers=headers).status_code == 409
    assert store.read(d / "material_last_export.json")["selection_hash"] == plan["selection_hash"]


def test_same_job_cannot_resume_twice_across_old_and_new_entries(studio, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    _, _, root = studio
    legacy_job(root, "resume-race", "interrupted")
    monkeypatch.setattr(appmod, "_jobs", {})
    monkeypatch.setattr(appmod, "_QUEUE", [])
    monkeypatch.setattr(appmod, "_RUNNING", {"job_id": None})
    monkeypatch.setattr(appmod, "_build_resume_args", lambda jid: ((jid,), None))
    launched = []
    monkeypatch.setattr(appmod, "threading", SimpleNamespace(Thread=lambda **kw: SimpleNamespace(start=lambda: launched.append(kw["args"]))))
    def post(path):
        client = appmod.app.test_client()
        with client.session_transaction() as session:
            session.update(authenticated=True, material_csrf="token")
        return client.post(path, headers={"X-Material-CSRF":"token"}).status_code
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(post, ["/api/resume/resume-race", "/api/material-jobs/resume-race/resume"]))
    assert sorted(results) == [200,409] and launched == [("resume-race",)]


def test_generation_board_reports_guard_intake_without_private_details(studio, monkeypatch):
    from types import SimpleNamespace
    client, _, _ = studio
    monkeypatch.setattr(appmod, "_deploy_guard", SimpleNamespace(intake_open=lambda: False), raising=False)
    board = client.get("/api/material-generation-board").get_json()
    assert board["accepting"] is False
    assert set(board) == {"running", "waiting", "accepting", "other_operations"}


def test_individual_work_counter_includes_legacy_route_and_releases_on_failure(studio, monkeypatch):
    client, _, _ = studio
    def individual_job(job_id, no):
        with appmod.app.test_client() as other:
            with other.session_transaction() as session:
                session["authenticated"] = True
            assert other.get("/api/material-generation-board").get_json()["other_operations"] == 1
        return appmod.jsonify(error="synthetic failure"), 500
    monkeypatch.setitem(appmod.app.view_functions, "api_regenerate", individual_job)
    assert client.post("/api/regenerate/synthetic/1").status_code == 500
    assert client.get("/api/material-generation-board").get_json()["other_operations"] == 0


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
def legacy_job(root, name, status="completed", channel="keizai", updated="2026-10-01T01:00:00Z"):
    d = root / "output" / name
    d.mkdir()
    store.save(d / "job.json", {"status": status, "channel_id": channel, "title_override": name + "の原稿",
                               "updated_at": updated, "generated": 3})
    if status == "completed":
        store.save(d / "manifest.json", {"channel_id": channel, "title": name + "の完成原稿",
                                       "generated": 3, "total_sentences": 8})
    else:
        (d / "manuscript.txt").write_text("保存された原稿です。" * 100, encoding="utf-8")
    return d


def test_history_includes_legacy_scoped_sorted_and_read_only(studio):
    import material_history
    client, _, root = studio
    legacy_job(root, "older")
    legacy_job(root, "stopped", "error", updated="2026-10-02T02:00:00Z")
    legacy_job(root, "other-channel", channel="roshia")
    legacy_job(root, "scene_fix_old")
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    history = material_history.index(appmod, "economy")
    assert [j["id"] for j in history["jobs"]] == ["stopped", "older"]
    assert history["jobs"][0]["href"] == "/progress/stopped"
    assert history["jobs"][0]["date"] == "2026/10/02 11:00"
    assert history["jobs"][0]["resumable"]
    assert history["jobs"][1]["href"] == "/materials/jobs/older"
    assert not history["jobs"][1]["resumable"]
    html = client.get("/materials?channel=economy").text
    assert 'data-job="older"' in html and "other-channel" not in html and "scene_fix_old" not in html
    assert "stoppedの原稿" in html and "従来のセンテンス" in html
    assert before == {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_studio_history_preserves_selection_and_does_not_duplicate_project_jobs(studio):
    import material_history
    client, _, root = studio
    d, p, original = make_job(root)
    choose(d, original)
    history = material_history.index(appmod, "economy")
    assert len(history["jobs"]) == 1 and not history["drafts"]
    assert history["jobs"][0]["selected"] == 1
    assert history["jobs"][0]["source_label"] == "素材スタジオ"
    html = client.get("/materials?channel=economy").text
    assert html.count('data-job="sample"') == 1
    assert p["project_id"] in html and "同じ案件に原稿を追加" in html
    assert store.load_library(d)["selections"]["1"] == original["id"]


def test_unreadable_history_keeps_other_jobs_and_missing_jobs_keep_project(studio):
    import material_history
    client, _, root = studio
    legacy_job(root, "valid")
    broken = legacy_job(root, "broken")
    (broken / "job.json").write_text("{updating", encoding="utf-8")
    store.save(root / "material_projects" / "bad.json", [])
    project = store.create_project(root, "economy", "原稿待ちの案件", {})
    missing = store.create_project(root, "economy", "保存された案件", {})
    missing["jobs"] = ["removed-job"]
    store.save(store.project_file(root, missing["project_id"]), missing)
    history = material_history.index(appmod, "economy")
    assert history["warning"] and len(history["jobs"]) == 2
    item = next(j for j in history["jobs"] if j["id"] == "broken")
    assert item["status"] == "unknown" and not item["resumable"]
    assert {p["project_id"] for p in history["drafts"]} == {project["project_id"], missing["project_id"]}
    response = client.get("/materials?channel=economy")
    assert response.status_code == 200 and "一部の制作記録" in response.text
    assert "/materials/jobs/removed-job" not in response.text


def test_history_keeps_jobs_beyond_old_thirty_item_limit(studio):
    import material_history
    client, _, root = studio
    for i in range(35):
        legacy_job(root, "job-" + str(i).zfill(2))
    history = material_history.index(appmod, "economy")
    assert len(history["jobs"]) == 35
    html = client.get("/materials?channel=economy").text
    assert html.count('class="job-row"') == 35
    assert "history-search" in html and "history-more" in html


def test_card_returns_to_recent_stage_and_skips_missing_job(studio):
    client, _, root = studio
    project = store.create_project(root, "economy", "同じ案件", {}, "https://trello.com/c/Card1234")
    complete = legacy_job(root, "complete")
    pending = legacy_job(root, "pending", "queued", updated="2030-10-02T01:00:00Z")
    store.bind_job(root, complete, project)
    store.bind_job(root, pending, project)
    project = store.get_project(root, project["project_id"])
    project["jobs"].append("does-not-exist")
    store.save(store.project_file(root, project["project_id"]), project)
    response = client.get("/materials?channel=economy&trello=https://trello.com/c/Card1234")
    assert response.status_code == 302 and response.headers["Location"] == "/progress/pending"


def test_resume_is_explicit_csrf_protected_and_refuses_active_jobs(studio, monkeypatch):
    client, headers, root = studio
    legacy_job(root, "stopped", "error")
    calls = []
    def resume(job_id):
        calls.append(job_id)
        return appmod.jsonify(ok=True, redirect="/progress/" + job_id)
    monkeypatch.setattr(appmod, "api_resume", resume)
    client.get("/materials?channel=economy")
    assert not calls
    assert client.post("/api/material-jobs/stopped/resume").status_code == 403
    assert not calls
    assert client.post("/api/material-jobs/stopped/resume", headers=headers).status_code == 200
    assert calls == ["stopped"]
    for status in ("running", "queued", "completed", "unknown"):
        legacy_job(root, status, status)
        assert client.post("/api/material-jobs/" + status + "/resume", headers=headers).status_code == 409
    assert calls == ["stopped"]


def test_existing_logos_are_served_in_channel_entry(studio):
    client, _, _ = studio
    html = client.get("/materials").text
    assert '/static/channel-logos/roshia.png' in html and '/static/channel-logos/keizai.png' in html
    assert "channel-logos/china" not in html
    for name in ("roshia", "keizai"):
        response = client.get("/static/channel-logos/" + name + ".png")
        assert response.status_code == 200
        assert Image.open(io.BytesIO(response.data)).size == (320, 320)
