import threading
import json
from types import SimpleNamespace

import pytest
import app as appmod
import job_recovery
import material_store as store
from migration_entry import MigrationGate


def test_first_log_after_restart_appends_to_saved_history(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(appmod, "APP_PASSWORD", "")
    monkeypatch.setattr(appmod, "_job_logs", {})
    folder = saved(tmp_path, "resume-logs", {"status": "interrupted"})
    old = {"time": "11:32:15", "category": "system", "message": "元の工程", "detail": ""}
    store.save(folder / "logs.json", [old])
    appmod._add_log(folder.name, "system", "再開")
    logs = appmod.app.test_client().get(f"/api/logs/{folder.name}").get_json()
    assert [row["message"] for row in logs["logs"]] == ["元の工程", "再開"]
    assert store.read(folder / "logs.json") == logs["logs"]
    for since in ("-1", "NaN"):
        assert appmod.app.test_client().get(f"/api/logs/{folder.name}?since={since}").get_json() == logs


def test_brief_sharing_error_during_read_does_not_hide_existing_state(tmp_path, monkeypatch):
    from pathlib import Path
    from utils import load_json
    path = tmp_path / "job.json"
    store.save(path, {"status": "interrupted"})
    original = Path.read_text
    busy = [True]
    def read_busy_once(self, *args, **kwargs):
        if busy:
            busy.pop()
            raise PermissionError("sharing violation")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read_busy_once)
    assert store.read(path)["status"] == "interrupted"
    busy.append(True)
    assert load_json(path)["status"] == "interrupted"


def test_checkpoint_keeps_previous_bytes_if_serialization_fails(tmp_path):
    from utils import save_json
    path = tmp_path / "routes.json"
    old = '[{"番号": 1}]'.encode("utf-8")
    path.write_bytes(old)
    with pytest.raises(TypeError):
        save_json(path, [1, object()])
    assert path.read_bytes() == old
    assert list(tmp_path.iterdir()) == [path]


def test_readers_keep_last_complete_snapshot_during_overwrite(tmp_path, monkeypatch):
    from pathlib import Path
    from utils import save_json
    path = tmp_path / "routes.json"
    old, new = {"note": "前"}, {"note": "次"}
    path.write_text(json.dumps(old), encoding="utf-8")
    observed = []
    def pause_after_open(target, data, *args, **kwargs):
        with target.open("w", encoding="utf-8") as stream:
            observed.append(path.read_text(encoding="utf-8"))
            return stream.write(data)
    monkeypatch.setattr(Path, "write_text", pause_after_open)
    dump = json.dump
    def partial_dump(value, stream, **kwargs):
        stream.write(" ")
        stream.flush()
        observed.append(path.read_text(encoding="utf-8"))
        return dump(value, stream, **kwargs)
    monkeypatch.setattr(json, "dump", partial_dump)
    save_json(path, new)
    assert json.loads(path.read_text(encoding="utf-8")) == new
    assert observed and all(value and json.loads(value) == old for value in observed)


def test_new_job_can_resume_if_its_thread_cannot_start(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "APP_PASSWORD", "")
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(appmod, "_jobs", {})
    monkeypatch.setattr(appmod, "resolve_channel_keys", lambda c: {"gemini": "stub", "openai": "stub", "anthropic": ""})
    def refused(self):
        raise RuntimeError("thread unavailable")
    monkeypatch.setattr(threading.Thread, "start", refused)
    client = appmod.app.test_client()
    user_text = "テスト用の原稿。" * 30
    response = client.post("/start", data={"channel_id": "default", "manuscript_text": user_text})
    assert response.status_code == 503
    data = response.get_json()
    assert data["redirect"] == f"/progress/{data['job_id']}"
    assert client.get(f"/api/status/{data['job_id']}").get_json()["status"] == "interrupted"
    assert (tmp_path / data["job_id"] / "manuscript.txt").read_text(encoding="utf-8") == user_text
    args, error = appmod._build_resume_args(data["job_id"])
    assert not error and args[1] == user_text


def worker(root):
    return SimpleNamespace(OUTPUT_DIR=root, _jobs={}, _QUEUE=[], _RUNNING={"job_id": None},
                           _RESUME_LOCK=threading.Lock(), _QUEUE_LOCK=threading.Lock(),
                           _jobs_lock=threading.Lock())


def saved(root, name, state):
    folder = root / name
    folder.mkdir()
    store.save(folder / "job.json", state)
    return folder


def test_restart_reconciles_abandoned_jobs_and_preserves_assets_and_settings(tmp_path, monkeypatch):
    module = worker(tmp_path)
    interrupted = saved(tmp_path, "abandoned", {"status":"running", "provider":"gpt-image", "creator":"担当", "percent":48})
    (interrupted / "image.png").write_bytes(b"saved-image")
    (interrupted / "manuscript.txt").write_text("保存した原稿", encoding="utf-8")
    saved(tmp_path, "queued", {"status":"queued"})
    for status in ("completed", "error", "unknown"):
        saved(tmp_path, status, {"status":status})
    malformed = saved(tmp_path, "malformed", {}) / "job.json"
    malformed.write_text("broken-json", encoding="utf-8")
    module._jobs["owned"] = {"status":"running"}
    saved(tmp_path, "owned", {"status":"running"})
    saved(tmp_path, "owned-by-process", {"status":"running", "runtime_owner":{"pid":44}})
    monkeypatch.setattr(job_recovery, "owner_alive", lambda owner: owner == {"pid":44})
    prepare = job_recovery.WorkerRecovery(module)
    prepare()
    state = store.read(interrupted / "job.json")
    assert state["status"] == "interrupted" and state["recovery_previous_status"] == "running"
    assert state["provider"] == "gpt-image" and state["creator"] == "担当" and state["percent"] == 48
    assert (interrupted / "image.png").read_bytes() == b"saved-image"
    assert (interrupted / "manuscript.txt").read_text(encoding="utf-8") == "保存した原稿"
    assert store.read(tmp_path / "queued/job.json")["status"] == "interrupted"
    for status in ("completed", "error", "unknown"):
        assert store.read(tmp_path / status / "job.json")["status"] == status
    for name in ("owned", "owned-by-process"):
        assert store.read(tmp_path / name / "job.json")["status"] == "running"
    assert malformed.read_text(encoding="utf-8") == "broken-json"
    saved(tmp_path, "later", {"status":"running"})
    prepare()
    assert store.read(tmp_path / "later/job.json")["status"] == "running"  # no repeated scan
    monkeypatch.setattr(job_recovery, "owner_alive", lambda owner: False)
    prepare()
    assert store.read(tmp_path / "owned-by-process/job.json")["status"] == "interrupted"
    monkeypatch.setattr(job_recovery.os, "getpid", lambda: prepare.pid + 1)
    prepare()
    assert store.read(tmp_path / "later/job.json")["status"] == "interrupted"  # serving PID, not preload parent


def test_readonly_migration_never_reconciles(tmp_path):
    calls = []
    def app(environ, start):
        start("200 OK", [])
        return [b"ok"]
    gate = MigrationGate(app, tmp_path, "readonly", prepare=lambda: calls.append(True))
    gate({"REQUEST_METHOD":"GET", "PATH_INFO":"/version"}, lambda *args: None)
    assert not calls
    gate.mode = "active"
    (tmp_path / ".migration-readonly").touch()
    gate({"REQUEST_METHOD":"GET", "PATH_INFO":"/version"}, lambda *args: None)
    assert not calls
    (tmp_path / ".migration-readonly").unlink()
    gate({"REQUEST_METHOD":"GET", "PATH_INFO":"/version"}, lambda *args: None)
    assert calls == [True]


def test_first_update_after_restart_keeps_saved_job_parameters(tmp_path, monkeypatch):
    saved(tmp_path, "resume", {"status":"interrupted", "channel_id":"keizai", "provider":"gpt-image", "creator":"担当"})
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(appmod, "_jobs", {})
    appmod._set_job_state("resume", status="queued", percent=0)
    result = store.read(tmp_path / "resume/job.json")
    assert result["channel_id"] == "keizai" and result["provider"] == "gpt-image" and result["creator"] == "担当"
    assert result["runtime_owner"]["pid"] > 0


def test_worker_identity_distinguishes_pid_reuse_and_different_instances():
    owner = job_recovery.runtime_owner()
    assert job_recovery.owner_alive(owner)
    assert not job_recovery.owner_alive({**owner, "nonce":"a-previous-process"})
    assert not job_recovery.owner_alive({**owner, "instance":"a-replaced-instance"})
