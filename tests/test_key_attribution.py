"""本番キーの監査で秘密値や共通キーへの代替を見落とさない。外部APIなし。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as appmod


def test_audit_requires_configured_login(monkeypatch):
    monkeypatch.setattr(appmod, "APP_PASSWORD", "test-password")
    client = appmod.app.test_client()
    assert client.get("/api/key-attribution").status_code == 401
    monkeypatch.setattr(appmod, "APP_PASSWORD", "")
    assert client.get("/api/key-attribution").status_code == 403


def test_audit_reports_effective_keys_and_shared_fallback_without_secrets(monkeypatch):
    monkeypatch.setattr(appmod, "APP_PASSWORD", "test-password")
    monkeypatch.setattr(appmod, "load_channels", lambda: [
        {"id": "default", "api_env_prefix": ""},
        {"id": "keizai", "api_env_prefix": "KEIZAI"},
        {"id": "seikou", "api_env_prefix": "SEIKOU"},
    ])
    monkeypatch.setenv("GEMINI_API_KEY", "fake-common-ABCD")
    monkeypatch.setenv("KEIZAI_GEMINI_API_KEY", "fake-keizai-EFGH")
    monkeypatch.delenv("SEIKOU_GEMINI_API_KEY", raising=False)
    client = appmod.app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
    response = client.get("/api/key-attribution")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    rows = {row["channel_id"]: row for row in response.get_json()["channels"]}
    assert rows["keizai"]["key_suffix"] == "EFGH"
    assert rows["keizai"]["source_env"] == "KEIZAI_GEMINI_API_KEY"
    assert rows["keizai"]["common_fallback"] is False
    assert rows["seikou"]["common_fallback"] is True
    assert rows["seikou"]["shared_with_channels"] == ["default"]
    assert "fake-common-ABCD" not in response.get_data(as_text=True)
    assert "fake-keizai-EFGH" not in response.get_data(as_text=True)
    page = client.get("/settings/api-usage")
    assert page.status_code == 200
    assert "text/html" in page.content_type
    assert "…EFGH" in page.get_data(as_text=True)
    assert "fake-keizai-EFGH" not in page.get_data(as_text=True)
    monkeypatch.setenv("KEIZAI_GEMINI_API_KEY", " ")
    rows = {row["channel_id"]: row for row in client.get("/api/key-attribution").get_json()["channels"]}
    assert rows["keizai"]["key_suffix"] == "ABCD"
    assert rows["keizai"]["common_fallback"] is True
