import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as appmod  # noqa: E402


def test_recent_jobs_excludes_scene_fix_outputs(tmp_path, monkeypatch):
    out_root = tmp_path / "output"
    out_root.mkdir()
    monkeypatch.setattr(appmod, "OUTPUT_DIR", out_root)
    monkeypatch.setattr(appmod, "APP_PASSWORD", "")

    normal = out_root / "20260626_120000"
    normal.mkdir()
    (normal / "manifest.json").write_text(json.dumps({
        "title": "通常ジョブ",
        "generated": 3,
        "total_sentences": 4,
        "channel_id": "roshia",
    }, ensure_ascii=False), encoding="utf-8")
    (normal / "job.json").write_text(json.dumps({"status": "completed"}, ensure_ascii=False), encoding="utf-8")

    scene = out_root / "scene_fix_20260626_121000_abcdef"
    scene.mkdir()
    (scene / "manifest.json").write_text(json.dumps({
        "tool": "scene_fix",
        "job_id": scene.name,
        "sentence": "シーン直しジョブ",
    }, ensure_ascii=False), encoding="utf-8")

    client = appmod.app.test_client()
    resp = client.get("/")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "通常ジョブ" in html
    assert "scene_fix_20260626_121000_abcdef" not in html
    assert "シーン直しジョブ" not in html
