"""素材のジョブを、その案件（Trello のカード）の AI 呼び出しとして記録に残す（2026-10-22）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as appmod  # noqa: E402
import studio_operations  # noqa: E402
import subscription_runtime  # noqa: E402


def test_job_card_comes_from_the_material_context(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path)
    seen = []
    monkeypatch.setattr(subscription_runtime, "set_card", seen.append)
    (tmp_path / "job-a").mkdir()
    (tmp_path / "job-a" / "material_context.json").write_text(
        json.dumps({"trello_url": "https://trello.com/c/Ab3dE6gH", "project_id": "trello-Ab3dE6gH"}), encoding="utf-8")
    (tmp_path / "job-b").mkdir()
    appmod._set_job_card("job-a")
    appmod._set_job_card("job-b")  # 案件に結び付かないジョブ
    assert seen == ["https://trello.com/c/Ab3dE6gH", None]
    assert [subscription_runtime.clean_card(v) for v in seen] == ["Ab3dE6gH", ""]


def test_studio_operation_sets_the_card_before_running(monkeypatch):
    calls = []
    module = type("Module", (), {"_set_job_card": staticmethod(calls.append)})
    monkeypatch.setattr(studio_operations, "_replay", lambda *a: (_ for _ in ()).throw(RuntimeError("stop")))
    monkeypatch.setattr(studio_operations, "_write", lambda *a, **k: None)
    try:
        studio_operations._run(module, {"id": "op1", "job_id": "job-a"}, {})
    except Exception:
        pass
    assert calls == ["job-a"]
