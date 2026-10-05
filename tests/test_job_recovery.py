import threading
from types import SimpleNamespace

import app as appmod
import job_recovery
import material_store as store
from migration_entry import MigrationGate


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
