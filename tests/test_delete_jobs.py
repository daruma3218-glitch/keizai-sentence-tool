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


def test_delete_job_can_force_remove_running_job(monkeypatch, tmp_path):
    client, output_dir = _configure_tmp_app(monkeypatch, tmp_path)
    job_dir = output_dir / "20260620_120001"
    job_dir.mkdir()
    (job_dir / "job.json").write_text(
        json.dumps({"status": "running"}, ensure_ascii=False),
        encoding="utf-8",
    )

    res = client.delete("/api/jobs/20260620_120001")

    assert res.status_code == 200
    assert not job_dir.exists()


def test_delete_job_rejects_path_traversal(monkeypatch, tmp_path):
    client, output_dir = _configure_tmp_app(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    res = client.delete("/api/jobs/..%2Foutside")

    assert res.status_code == 404
    assert outside.exists()


def test_delete_job_refuses_a_job_running_or_queued_in_this_process(monkeypatch, tmp_path):
    # 2026-10-22：止める手段が無いので「削除」で他の人の生成中の回が消えないようにする
    client, output_dir = _configure_tmp_app(monkeypatch, tmp_path)
    for name in ("20261022_120000", "20261022_120001"):
        (output_dir / name).mkdir()
    with app_module._jobs_lock:
        app_module._jobs["20261022_120000"] = {"status": "running"}
    with app_module._QUEUE_LOCK:
        app_module._QUEUE.append("20261022_120001")
    try:
        for name in ("20261022_120000", "20261022_120001"):
            res = client.delete("/api/jobs/" + name)
            assert res.status_code == 409
            assert "削除できません" in res.get_json()["error"]
            assert (output_dir / name).exists()
    finally:
        with app_module._QUEUE_LOCK:
            app_module._QUEUE.remove("20261022_120001")


def test_regenerate_keeps_the_current_image_as_a_candidate(monkeypatch, tmp_path):
    from PIL import Image
    import material_store
    job_dir = tmp_path / "job"
    (job_dir / "images").mkdir(parents=True)
    Image.new("RGB", (8, 8), "red").save(job_dir / "images" / "3.png")
    (job_dir / "rows_progress.json").write_text(json.dumps({"rows": [{"no": 3, "filename": "3.png", "route": "diagram"}]}), encoding="utf-8")
    app_module._preserve_before_regen(job_dir, 3)
    app_module._preserve_before_regen(job_dir, 3)  # 同じ画像を2回残さない
    library = material_store.load_library(job_dir)
    assert [c["label"] for c in library["candidates"]["3"]] == ["作り直す前の素材"]
    app_module._preserve_before_regen(job_dir, 9)  # 行が無くても止まらない
