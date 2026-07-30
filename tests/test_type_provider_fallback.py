#!/usr/bin/env python3
"""タイプ別プロバイダのキー欠如時フォールバック＋図解gpt-image設定の pytest。

受け入れ基準:
- _effective_type_providers: キー未設定のプロバイダ指定は外れ（主プロバイダ代替）、
  キーがあればそのまま。代替内容がメモとして返る
- /start はタイプ別指定のキー欠如ではブロックしない（主プロバイダのキーのみ必須）
- channels.json: keizai / seikou の図解が gpt-image 指定
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as appmod  # noqa: E402


def test_effective_tp_drops_missing_key_provider():
    defaults = {"type_providers": {"diagram": "gpt-image", "realphoto": "nanobanana"}}
    # OPENAIキー無し → diagram指定は外れる。GEMINIあり → realphoto維持
    tp, notes = appmod._effective_type_providers(defaults, {"openai": "", "gemini": "g"})
    assert tp == {"realphoto": "nanobanana"}
    assert notes and "gpt-image" in notes[0] and "代替" in notes[0]


def test_effective_tp_keeps_when_keys_present():
    defaults = {"type_providers": {"diagram": "gpt-image", "realphoto": "nanobanana"}}
    tp, notes = appmod._effective_type_providers(defaults, {"openai": "o", "gemini": "g"})
    assert tp == {"diagram": "gpt-image", "realphoto": "nanobanana"}
    assert notes == []


def test_effective_tp_empty_and_none_safe():
    assert appmod._effective_type_providers({}, {}) == ({}, [])
    assert appmod._effective_type_providers({"type_providers": None}, None) == ({}, [])


def test_channels_diagram_uses_gpt_image():
    d = json.load(open(Path(__file__).resolve().parent.parent / "channels.json", encoding="utf-8"))
    by_id = {c["id"]: c["defaults"] for c in d["channels"]}
    assert by_id["keizai"]["type_providers"]["diagram"] == "gpt-image"
    assert by_id["seikou"]["type_providers"]["diagram"] == "gpt-image"
    # 主プロバイダは nanobanana のまま（実写風・イラストは従来どおり）
    assert by_id["keizai"]["provider"] == "nanobanana"
    assert by_id["seikou"]["provider"] == "nanobanana"


def test_start_not_blocked_by_type_provider_key(monkeypatch, tmp_path):
    """seikou で OPENAI 未設定でも /start がキーエラーで止まらない（主プロバイダのキーのみ必須）。"""
    monkeypatch.setattr(appmod, "APP_PASSWORD", "pw")
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path)
    # seikou: SEIKOU_* 無し・共通は GEMINI/ANTHROPIC のみ設定
    for k in list(__import__("os").environ):
        pass
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SEIKOU_OPENAI_API_KEY", raising=False)

    started = {}
    monkeypatch.setattr(appmod, "_run_pipeline_thread", lambda *a, **k: started.setdefault("ok", True))

    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()
    with c.session_transaction() as s:
        s["authenticated"] = True
    res = c.post("/start", data={"channel_id": "seikou", "manuscript_text": "こ" * 200})
    body = res.get_json() or {}
    assert res.status_code == 200, f"キー欠如でブロックされない: {body}"
    assert "OPENAI" not in json.dumps(body)