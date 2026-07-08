#!/usr/bin/env python3
"""未ログイン時のAPI応答（通信エラー対策）の pytest。

受け入れ基準:
- 未ログインで /start や /api/* を叩くと、HTMLリダイレクトではなく JSON 401 を返す
  （フロントの「Unexpected token '<' ... is not valid JSON」を撲滅）
- 画面系（/ や /progress）は従来どおりログインページへリダイレクト
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as appmod  # noqa: E402


def _client(monkeypatch):
    monkeypatch.setattr(appmod, "APP_PASSWORD", "testpass")
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def test_api_returns_json_401_when_not_logged_in(monkeypatch):
    c = _client(monkeypatch)
    res = c.post("/start", data={})
    assert res.status_code == 401
    assert "ログイン" in res.get_json()["error"]

    res = c.get("/api/rows/20260708_000000")
    assert res.status_code == 401
    assert res.is_json

    res = c.post("/api/resume/20260708_000000")
    assert res.status_code == 401
    assert res.is_json


def test_page_routes_still_redirect_to_login(monkeypatch):
    c = _client(monkeypatch)
    res = c.get("/", follow_redirects=False)
    assert res.status_code in (301, 302)
    assert "/login" in res.headers.get("Location", "")
