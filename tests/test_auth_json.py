#!/usr/bin/env python3
"""未ログイン時のAPI応答（通信エラー対策）の pytest。

受け入れ基準:
- 未ログインで /start や /api/* を叩くと、HTMLリダイレクトではなく JSON 401 を返す
  （フロントの「Unexpected token '<' ... is not valid JSON」を撲滅）
- 画面系（/ や /progress）は従来どおりログインページへリダイレクト
"""
import sys
from pathlib import Path

import pytest

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


def test_logout_clears_session_and_relogin_returns_to_material_studio(monkeypatch):
    c = _client(monkeypatch)
    with c.session_transaction() as state:
        state.update(authenticated=True, login_next="/", material_csrf="old-token")
    response = c.get("/logout")
    assert response.headers["Location"] == "/materials"
    with c.session_transaction() as state:
        assert not state

    response = c.get(response.headers["Location"], follow_redirects=True)
    assert response.request.path == "/login"
    assert "ログイン - 素材スタジオ" in response.text
    assert "センテンスつくーる" not in response.text
    assert c.get("/api/rows/example").status_code == 401

    response = c.post("/login", data={"password": "wrong"})
    assert "パスワードが正しくありません" in response.text
    with c.session_transaction() as state:
        assert not state.get("authenticated")
        assert state["login_next"] == "/materials"
    response = c.post("/login", data={"password": "testpass"})
    assert response.headers["Location"] == "/materials"


def test_direct_login_defaults_to_material_studio(monkeypatch):
    c = _client(monkeypatch)
    assert c.post("/login", data={"password": "testpass"}).headers["Location"] == "/materials"
    assert c.get("/login").headers["Location"] == "/materials"


def test_password_disabled_login_and_logout_return_to_material_studio(monkeypatch):
    c = _client(monkeypatch)
    monkeypatch.setattr(appmod, "APP_PASSWORD", "")
    assert c.get("/login").headers["Location"] == "/materials"
    assert c.get("/logout").headers["Location"] == "/materials"


@pytest.mark.parametrize("destination", ["/materials?channel=russia", "/progress/example", "/"])
def test_login_preserves_explicit_internal_destination(monkeypatch, destination):
    c = _client(monkeypatch)
    assert c.get(destination).headers["Location"] == "/login"
    assert c.post("/login", data={"password": "testpass"}).headers["Location"] == destination


@pytest.mark.parametrize("destination", ["https://example.com", "//example.com", "/\\example.com"])
def test_invalid_login_destination_falls_back_to_material_studio(monkeypatch, destination):
    c = _client(monkeypatch)
    with c.session_transaction() as state:
        state["login_next"] = destination
    assert c.post("/login", data={"password": "testpass"}).headers["Location"] == "/materials"
