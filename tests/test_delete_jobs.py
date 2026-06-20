import json

import app as app_module


def _configure_tmp_app(monkeypatch, tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    monkeypatch.setattr(app_module, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(app_module, "APP_PASSWORD", "")
    with app_module._jobs_lock:
        app_module._jobs.clear()
        app_module._job_logs.clear()
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client(), output_dir


def test_delete_job_removes_output_directory(monkeypatch, tmp_path):
    client, output_dir = _configure_tmp_app(monkeypatch, tmp_path)
    job_dir = output_dir / "20260620_120000"
    images_dir = job_dir / "images"
    images_dir.mkdir(parents=True)
    (images_dir / "001.png").write_bytes(b"fake")
    (job_dir / "job.json").write_text(
        json.dumps({"status": "completed"}, ensure_ascii=False),
        encoding="utf-8",
    )

    res = client.delete("/api/jobs/20260620_120000")

    assert res.status_code == 200
    assert res.get_json()["ok"] is True
    assert not job_dir.exists()


def test_delete_job_blocks_running_job(monkeypatch, tmp_path):
    client, output_dir = _configure_tmp_app(monkeypatch, tmp_path)
    job_dir = output_dir / "20260620_120001"
    job_dir.mkdir()
    (job_dir / "job.json").write_text(
        json.dumps({"status": "running"}, ensure_ascii=False),
        encoding="utf-8",
    )

    res = client.delete("/api/jobs/20260620_120001")

    assert res.status_code == 409
    assert job_dir.exists()


def test_delete_job_rejects_path_traversal(monkeypatch, tmp_path):
    client, output_dir = _configure_tmp_app(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    res = client.delete("/api/jobs/..%2Foutside")

    assert res.status_code == 404
    assert outside.exists()
