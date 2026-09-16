#!/usr/bin/env python3
"""OpenAI 画像モデル選択（gpt-image-2 / 2.5 Flare / 2.5 Sunburst）の pytest。

受け入れ基準:
- resolve_openai_image_model: 有効IDを優先順に採用、未知IDは無視、無ければ環境変数→既定
- /start: フォームの openai_model が job.json に保存され、スレッドへ渡る。未知IDは既定に丸める
- upload.html に選択肢が描画される
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as appmod  # noqa: E402
import generator  # noqa: E402


def test_resolve_model_priority(monkeypatch):
    monkeypatch.delenv("OPENAI_IMAGE_MODEL", raising=False)
    assert generator.resolve_openai_image_model("gpt-image-2.5-sunburst", "gpt-image-2") == "gpt-image-2.5-sunburst"
    assert generator.resolve_openai_image_model("bogus-model", "gpt-image-2.5-flare") == "gpt-image-2.5-flare"
    assert generator.resolve_openai_image_model(None, "", "  ") == "gpt-image-2"
    monkeypatch.setenv("OPENAI_IMAGE_MODEL", "gpt-image-2.5-flare")
    assert generator.resolve_openai_image_model(None) == "gpt-image-2.5-flare"
    monkeypatch.setenv("OPENAI_IMAGE_MODEL", "not-a-real-model")
    assert generator.resolve_openai_image_model(None) == "gpt-image-2"


def test_start_persists_and_passes_openai_model(monkeypatch, tmp_path):
    monkeypatch.setattr(appmod, "APP_PASSWORD", "pw")
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    captured = {}
    monkeypatch.setattr(appmod, "_run_pipeline_thread",
                        lambda *a, **k: captured.update({"args": a, "kwargs": k}))
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()
    with c.session_transaction() as s:
        s["authenticated"] = True

    res = c.post("/start", data={"channel_id": "keizai", "manuscript_text": "こ" * 200,
                                 "openai_model": "gpt-image-2.5-sunburst"})
    assert res.status_code == 200, res.get_json()
    job_id = res.get_json()["job_id"]
    import time
    for _ in range(50):  # スレッド起動待ち
        if "kwargs" in captured:
            break
        time.sleep(0.05)
    assert captured["kwargs"].get("openai_model") == "gpt-image-2.5-sunburst"
    state = json.loads((tmp_path / job_id / "job.json").read_text(encoding="utf-8"))
    assert state["openai_model"] == "gpt-image-2.5-sunburst"

    # 未知IDは既定に丸める（本番で即エラーにしない）
    captured.clear()
    res = c.post("/start", data={"channel_id": "keizai", "manuscript_text": "こ" * 200,
                                 "openai_model": "gpt-image-9-nonexistent"})
    assert res.status_code == 200
    for _ in range(50):
        if "kwargs" in captured:
            break
        time.sleep(0.05)
    assert captured["kwargs"].get("openai_model") == "gpt-image-2"


def test_upload_page_lists_models(monkeypatch):
    monkeypatch.setattr(appmod, "APP_PASSWORD", "pw")
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()
    with c.session_transaction() as s:
        s["authenticated"] = True
    html = c.get("/").get_data(as_text=True)
    assert 'name="openai_model"' in html
    for m in ("gpt-image-2.5-flare", "gpt-image-2.5-sunburst"):
        assert m in html
