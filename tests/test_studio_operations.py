"""時間のかかる操作の「受け付け → 問い合わせ」（2026-10-14）。外部API・AIは呼ばない。

新しい入口（Cloudflare）は応答を約100秒しか待たない。印（X-Studio-Async）の付いた要求だけ、受け付けてすぐ番号を返し、
今までと同じ入口を裏で動かして、結果を問い合わせで返す。印の無い要求は今までどおり待って返す。
"""
import io
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as appmod  # noqa: E402
import deploy_guard  # noqa: E402
import generator  # noqa: E402
import material_routes  # noqa: E402
import material_store as store  # noqa: E402
import studio_operations as ops  # noqa: E402
from test_materials import make_job  # noqa: E402

CHANNEL = {"id": "roshia", "defaults": {"style_preset": "flat_infographic",
                                        "type_providers": {"diagram": "gpt-image"}}}


@pytest.fixture
def studio(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path / "output")
    appmod.OUTPUT_DIR.mkdir()
    monkeypatch.setattr(appmod, "APP_PASSWORD", "test-password")
    appmod.app.config.update(TESTING=True)
    material_routes.ACTIVE.clear()
    ops._RUNNING.clear()
    ops._STARTED.clear()
    ops._UNSAVED.clear()
    client = appmod.app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["material_csrf"] = "offline-token"
    yield client, tmp_path
    deadline = time.time() + 10
    while ops.running_count() and time.time() < deadline:
        time.sleep(0.02)  # 裏の処理が終わってから一時フォルダを消す


def png(path):
    Image.new("RGB", (32, 18), (255, 255, 255)).save(path)


def async_headers(op_id, **extra):
    return {"X-Studio-Async": "1", "X-Studio-Operation": op_id, **extra}


def wait(client, op_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get("/api/operations/" + op_id)
        state = response.get_json()
        if response.status_code != 200 or state.get("status") != "running":
            settle()
            return response.status_code, state
        time.sleep(0.02)
    raise AssertionError("operation did not finish")


def settle(timeout=10):
    """結果を保存した後に、動いている一覧から外れるまで待つ（保存 → 一覧から外す、の順のため）。"""
    deadline = time.time() + timeout
    while ops.running_count() and time.time() < deadline:
        time.sleep(0.01)
    assert ops.running_count() == 0


def fake_scene_fix(monkeypatch, gate=None, calls=None):
    monkeypatch.setattr(appmod, "get_channel", lambda channel_id: CHANNEL)
    monkeypatch.setattr(appmod, "resolve_channel_keys", lambda channel: {"gemini": "g", "openai": "o", "anthropic": "a"})

    def generate(prompts, output_dir, **kwargs):
        if calls is not None:
            calls.append(kwargs)
        if gate is not None:
            assert gate.wait(10)
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        out = []
        for p in prompts:
            png(Path(output_dir) / f"{p['index']}.png")
            out.append({"index": p["index"], "success": True, "filename": f"{p['index']}.png", "provider": kwargs.get("provider")})
        return out
    monkeypatch.setattr(generator, "run_parallel_generation", generate)


SCENE = {"channel_id": "roshia", "route": "diagram", "variant_count": "2", "provider": "auto",
         "sentence": "ベラルーシはロシアへの経済依存を深めました。", "fix_mode": "balanced"}


def test_scene_fix_is_accepted_at_once_and_returns_the_same_result(studio, monkeypatch):
    client, _ = studio
    fake_scene_fix(monkeypatch)
    accepted = client.post("/api/scene-fix", data=SCENE, headers=async_headers("scene-fix-operation-1"))
    assert accepted.status_code == 202
    assert accepted.get_json()["operation"] == {"id": "scene-fix-operation-1", "status": "running",
                                                "poll": "/api/operations/scene-fix-operation-1"}
    code, state = wait(client, "scene-fix-operation-1")
    assert code == 200 and state["status"] == "done"
    assert state["result"]["status"] == 200
    body = state["result"]["body"]
    assert body["ok"] is True and body["tool"] == "scene_fix" and len(body["variants"]) == 2
    # 印の無い要求（古い画面・既存の試験）は今までどおり、その場で結果を返す
    direct = client.post("/api/scene-fix", data=SCENE)
    assert direct.status_code == 200 and set(direct.get_json()) == set(body)


def test_reference_image_reaches_the_background_request(studio, monkeypatch):
    client, _ = studio
    calls = []
    fake_scene_fix(monkeypatch, calls=calls)
    picture = io.BytesIO()
    Image.new("RGB", (16, 9), "red").save(picture, format="PNG")
    data = dict(SCENE, reference_image=(io.BytesIO(picture.getvalue()), "source.png"))
    assert client.post("/api/scene-fix", data=data, content_type="multipart/form-data",
                       headers=async_headers("scene-fix-with-picture")).status_code == 202
    code, state = wait(client, "scene-fix-with-picture")
    assert state["result"]["status"] == 200
    edit = Path(calls[0]["edit_image_path"])
    assert edit.read_bytes() == picture.getvalue()


def test_same_number_runs_once_even_if_sent_again(studio, monkeypatch):
    client, _ = studio
    gate, calls = threading.Event(), []
    fake_scene_fix(monkeypatch, gate=gate, calls=calls)
    first = client.post("/api/scene-fix", data=SCENE, headers=async_headers("same-number-operation"))
    again = client.post("/api/scene-fix", data=SCENE, headers=async_headers("same-number-operation"))
    assert first.status_code == again.status_code == 202
    assert client.get("/api/operations/same-number-operation").get_json()["status"] == "running"
    gate.set()
    assert wait(client, "same-number-operation")[1]["status"] == "done"
    finished = client.post("/api/scene-fix", data=SCENE, headers=async_headers("same-number-operation"))
    assert finished.status_code == 202 and finished.get_json()["operation"]["status"] == "done"
    assert len(calls) == 1


def test_queue_limit_and_deployment_waits_for_background_work(studio, monkeypatch):
    client, _ = studio
    monkeypatch.setenv("STUDIO_OPERATIONS_MAX", "1")
    monkeypatch.setenv("STUDIO_OPERATIONS_QUEUE", "2")
    gate, calls = threading.Event(), []
    fake_scene_fix(monkeypatch, gate=gate, calls=calls)
    module = SimpleNamespace(_jobs_lock=threading.Lock(), _jobs={}, OUTPUT_DIR=appmod.OUTPUT_DIR)
    assert deploy_guard.jobs_busy_for(module) is False
    assert client.post("/api/scene-fix", data=SCENE, headers=async_headers("queue-limit-operation-1")).status_code == 202
    assert deploy_guard.jobs_busy_for(module) is True  # 受け付けの応答を返した後も、反映は待つ
    # 同時に動かすのは1件。2件目は断らずに順番待ち、3件目は受け付けない
    assert client.post("/api/scene-fix", data=SCENE, headers=async_headers("queue-limit-operation-2")).status_code == 202
    deadline = time.time() + 5
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    second = client.get("/api/operations/queue-limit-operation-2").get_json()
    assert second["status"] == "running" and second["waiting"] is True
    crowded = client.post("/api/scene-fix", data=SCENE, headers=async_headers("queue-limit-operation-3"))
    assert crowded.status_code == 429 and "混み合って" in crowded.get_json()["error"]
    assert len(calls) == 1
    gate.set()
    assert wait(client, "queue-limit-operation-1")[1]["status"] == "done"
    assert wait(client, "queue-limit-operation-2")[1]["status"] == "done"
    assert len(calls) == 2 and deploy_guard.jobs_busy_for(module) is False


def test_revise_runs_in_background(studio, monkeypatch):
    client, _ = studio
    fake_scene_fix(monkeypatch)
    first = client.post("/api/scene-fix", data=SCENE).get_json()
    accepted = client.post(f"/api/scene-fix/{first['job_id']}/revise", data={"index": "1", "instruction": "矢印を太く"},
                           headers=async_headers("revise-operation-1"))
    assert accepted.status_code == 202
    code, state = wait(client, "revise-operation-1")
    body = state["result"]["body"]
    assert state["result"]["status"] == 200 and body["revision"]["revision_no"] == 1 and body["revision"]["source_index"] == 1


def test_request_is_passed_through_as_sent(studio, monkeypatch):
    """作り直さずにそのまま渡す。ファイル名の書き方（" を含む）・クエリも、印の無い時と同じに扱われる。"""
    client, _ = studio
    calls = []
    fake_scene_fix(monkeypatch, calls=calls)
    picture = io.BytesIO()
    Image.new("RGB", (16, 9), "red").save(picture, format="PNG")
    # ブラウザ（Chrome）と同じ形の本文：ファイル名の " は %22 で送られる
    boundary = "----WebKitFormBoundaryStudioTest"
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in SCENE.items()]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="reference_image"; filename="a%22b.png"\r\n'
                 f'Content-Type: image/png\r\n\r\n'.encode() + picture.getvalue() + b"\r\n")
    raw = b"".join(parts) + f"--{boundary}--\r\n".encode()

    def send(headers=None):
        return client.post("/api/scene-fix?from=test&x=%E3%81%82", data=raw,
                           content_type="multipart/form-data; boundary=" + boundary, headers=headers or {})
    direct = send()
    assert direct.status_code == 200
    assert send(async_headers("passed-through-operation")).status_code == 202
    code, state = wait(client, "passed-through-operation")
    assert state["result"]["status"] == 200
    assert len(calls) == 2
    sync_ref, async_ref = (Path(c["edit_image_path"]).name if c["edit_image_path"] else None for c in calls)
    assert sync_ref == async_ref and async_ref is not None


def test_production_mode_errors_keep_the_same_500(studio, monkeypatch):
    client, _ = studio
    appmod.app.config.update(TESTING=False)
    try:
        def explode(channel_id):
            raise RuntimeError("provider secret details")
        monkeypatch.setattr(appmod, "get_channel", explode)
        direct = client.post("/api/scene-fix", data=SCENE)
        assert client.post("/api/scene-fix", data=SCENE, headers=async_headers("production-error-op")).status_code == 202
        code, state = wait(client, "production-error-op")
        assert state["result"]["status"] == direct.status_code == 500
        assert "secret" not in json.dumps(state)
    finally:
        appmod.app.config.update(TESTING=True)


def test_first_login_with_cloudflare_proof_only(studio, monkeypatch):
    """新しい入口の初回：セッションの Cookie が無く、Cloudflare の証明だけで来ても、裏の要求も証明で通る。"""
    _, _ = studio
    fake_scene_fix(monkeypatch)
    import cloudflare_access
    monkeypatch.setattr(cloudflare_access, "verified_email",
                        lambda headers: "someone@example.com" if headers.get(cloudflare_access.HEADER) == "valid-proof" else "")
    fresh = appmod.app.test_client()
    proof = {cloudflare_access.HEADER: "valid-proof"}
    accepted = fresh.post("/api/scene-fix", data=SCENE, headers=async_headers("first-login-operation", **proof))
    assert accepted.status_code == 202
    code, state = wait(fresh, "first-login-operation")
    assert state["result"]["status"] == 200 and state["result"]["body"]["ok"] is True
    assert appmod.app.test_client().get("/api/operations/first-login-operation").status_code == 401


def test_result_is_kept_in_memory_when_saving_fails(studio, monkeypatch):
    client, _ = studio
    fake_scene_fix(monkeypatch)
    original = ops._write

    def fail_on_done(module, op_id, record):
        if record.get("status") == "done":
            raise OSError("disk full")
        return original(module, op_id, record)
    monkeypatch.setattr(ops, "_write", fail_on_done)
    assert client.post("/api/scene-fix", data=SCENE, headers=async_headers("unsaved-result-operation")).status_code == 202
    code, state = wait(client, "unsaved-result-operation")
    assert state["status"] == "done" and state["result"]["status"] == 200  # 「中断」と見誤らない
    again = client.post("/api/scene-fix", data=SCENE, headers=async_headers("unsaved-result-operation"))
    assert again.status_code == 202 and again.get_json()["operation"]["status"] == "done"


def test_live_owner_is_not_interrupted_until_too_old(studio):
    import os
    client, root = studio
    folder = root / "operations"
    live = {"instance": os.environ.get("RENDER_INSTANCE_ID", "local"), "pid": os.getppid(), "nonce": "other-process"}
    fresh = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    store.save(folder / "live-owner-operation.json", {"id": "live-owner-operation", "kind": "scene_fix",
               "status": "running", "owner": live, "started_at": fresh})
    assert client.get("/api/operations/live-owner-operation").get_json()["status"] == "running"
    old = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - ops.MAX_AGE_SECONDS - 60))
    store.save(folder / "old-owner-operation.json", {"id": "old-owner-operation", "kind": "scene_fix",
               "status": "running", "owner": live, "started_at": old})
    assert client.get("/api/operations/old-owner-operation").get_json()["status"] == "interrupted"


def test_unreadable_record_is_reported_as_finished(studio):
    client, root = studio
    folder = root / "operations"
    folder.mkdir()
    (folder / "broken-record-operation.json").write_text("{not json", encoding="utf-8")
    response = client.get("/api/operations/broken-record-operation")
    assert response.status_code == 200
    assert response.get_json()["status"] == "unknown" and "再読み込み" in response.get_json()["error"]


def test_too_large_request_keeps_413(studio, monkeypatch):
    client, root = studio
    fake_scene_fix(monkeypatch)
    monkeypatch.setitem(appmod.app.config, "MAX_CONTENT_LENGTH", 1000)
    big = dict(SCENE, extra_instruction="あ" * 2000)
    assert client.post("/api/scene-fix", data=big, headers=async_headers("too-large-operation")).status_code == 413
    assert ops.running_count() == 0
    assert not (root / "operations" / "too-large-operation.json").exists()


def test_login_csrf_and_numbers_are_checked_before_accepting(studio, monkeypatch, tmp_path):
    client, root = studio
    fake_scene_fix(monkeypatch)
    stranger = appmod.app.test_client()
    assert stranger.post("/api/scene-fix", data=SCENE, headers=async_headers("stranger-operation-1")).status_code == 401
    assert stranger.get("/api/operations/stranger-operation-1").status_code == 401
    d, _, original = make_job(root)
    body = {"request_id": "wrongcsrf", "no": "1", "kind": "generate", "revision": "2"}
    wrong = client.post("/api/material-jobs/sample/variant", data=body,
                        headers=async_headers("wrong-csrf-operation", **{"X-Material-CSRF": "other"}))
    assert wrong.status_code == 403  # その場で今までどおり断る（受け付けない）
    assert client.post("/api/scene-fix", data=SCENE, headers=async_headers("short")).status_code == 400
    assert client.get("/api/operations/short").status_code == 404
    assert client.get("/api/operations/never-accepted-operation").status_code == 404
    assert not (root / "operations").exists() or not list((root / "operations").glob("*.json"))


def test_regenerate_in_background_and_interrupted_row_is_released(studio, monkeypatch):
    client, root = studio
    job = root / "output" / "regen-job"
    (job / "images").mkdir(parents=True)
    store.save(job / "job.json", {"status": "completed", "channel_id": "roshia"})
    store.save(job / "prompts.json", {"rows": [{"no": 3, "prompt": "draw", "route": "diagram", "sentence": "文"}]})
    store.save(job / "rows_progress.json", {"rows": [{"no": 3, "route": "diagram", "status": "ok", "sentence": "文"}]})
    monkeypatch.setattr(appmod, "get_channel", lambda channel_id: CHANNEL)
    monkeypatch.setattr(appmod, "resolve_channel_keys", lambda channel: {"gemini": "g", "openai": "o", "anthropic": "a"})

    def generate(prompts, output_dir, **kwargs):
        png(Path(output_dir) / "3.png")
        return [{"index": 3, "success": True, "filename": "3.png"}]
    monkeypatch.setattr(generator, "run_parallel_generation", generate)
    assert client.post("/api/regenerate/regen-job/3", data={}, headers=async_headers("regenerate-operation")).status_code == 202
    code, state = wait(client, "regenerate-operation")
    assert state["result"]["status"] == 200 and state["result"]["body"]["filename"] == "3.png"

    # 前のプロセスが作り直しの途中で止まった：「中断」と返し、行の「生成中」を失敗に戻す（作り直さない）
    rows = json.loads((job / "rows_progress.json").read_text(encoding="utf-8"))
    rows["rows"][0]["status"] = "generating"
    (job / "rows_progress.json").write_text(json.dumps(rows), encoding="utf-8")
    store.save(root / "operations" / "stopped-regenerate-operation.json", {
        "id": "stopped-regenerate-operation", "kind": "regenerate", "path": "/api/regenerate/regen-job/3",
        "status": "running", "job_id": "regen-job", "no": 3, "started_at": "2026-10-14T00:00:00+00:00",
        "owner": {"instance": "another-instance", "pid": 4242, "nonce": "gone"}})
    code, state = wait(client, "stopped-regenerate-operation")
    assert state["status"] == "interrupted" and "自動では作り直しません" in state["error"]
    row = json.loads((job / "rows_progress.json").read_text(encoding="utf-8"))["rows"][0]
    assert row["status"] == "failed" and "中断" in row["error"]


def test_variant_in_background_and_interrupted_request_is_released(studio, monkeypatch):
    client, root = studio
    d, _, original = make_job(root)
    monkeypatch.setenv("KEIZAI_OPENAI_API_KEY", "offline-key")

    def generate(**kwargs):
        Image.new("RGB", (32, 18), "green").save(kwargs["output_dir"] / "two.png")
        return [{"success": True, "filename": "two.png"}]
    monkeypatch.setattr(generator, "run_parallel_generation", generate)
    body = {"request_id": "asyncvariant", "no": "1", "kind": "generate", "count": "1",
            "revision": str(store.load_library(d)["revision"])}
    accepted = client.post("/api/material-jobs/sample/variant", data=body,
                           headers=async_headers("variant-operation-1", **{"X-Material-CSRF": "offline-token"}))
    assert accepted.status_code == 202
    code, state = wait(client, "variant-operation-1")
    assert state["result"]["status"] == 200
    assert state["result"]["body"]["library"]["requests"]["asyncvariant"]["status"] == "completed"
    assert not material_routes.ACTIVE

    # 前のプロセスが別案の途中で止まった：受付の「実行中」を失敗に戻す（画面が作成中のまま固まらない）
    with store.LOCK:
        lib = store.load_library(d)
        lib["requests"]["stoppedvariant"] = {"status": "running", "kind": "generate", "no": "1",
                                             "requested_images": 1, "started_at": store.now(), "cost_amount": None}
        store.commit_library(d, lib, "operation_started", {"request_id": "stoppedvariant"})
    store.save(root / "operations" / "stopped-variant-operation.json", {
        "id": "stopped-variant-operation", "kind": "variant", "path": "/api/material-jobs/sample/variant",
        "status": "running", "job_id": "sample", "variant_request_id": "stoppedvariant",
        "started_at": "2026-10-14T00:00:00+00:00", "owner": {"instance": "another-instance", "pid": 4242, "nonce": "gone"}})
    assert ops.sweep(appmod) == ["stopped-variant-operation"]
    pending = store.load_library(d)["requests"]["stoppedvariant"]
    assert pending["status"] == "failed" and pending["interrupted"] is True
    assert client.get("/api/operations/stopped-variant-operation").get_json()["status"] == "interrupted"


def test_unexpected_errors_are_saved_without_details(studio, monkeypatch):
    client, root = studio

    def explode(channel_id):
        raise RuntimeError("provider secret details")
    monkeypatch.setattr(appmod, "get_channel", explode)  # 入口の外へ抜ける例外
    assert client.post("/api/scene-fix", data=SCENE, headers=async_headers("failing-operation-1")).status_code == 202
    code, state = wait(client, "failing-operation-1")
    assert state["status"] == "done" and state["result"]["status"] == 500
    assert "もう一度操作" in state["result"]["body"]["error"]
    saved = (root / "operations" / "failing-operation-1.json").read_text(encoding="utf-8")
    assert "secret" not in json.dumps(state) and "secret" not in saved


def test_old_records_are_pruned_but_running_ones_are_kept(studio):
    client, root = studio
    folder = root / "operations"
    folder.mkdir()
    old, fresh = folder / "old-finished-operation.json", folder / "fresh-finished-operation.json"
    for path in (old, fresh):
        path.write_text(json.dumps({"status": "done"}), encoding="utf-8")
    import os
    stale = time.time() - ops.KEEP_SECONDS - 60
    os.utime(old, (stale, stale))
    ops._prune(appmod, force=True)
    assert not old.exists() and fresh.exists()
