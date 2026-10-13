#!/usr/bin/env python3
"""新しい入口（sozai.apprendre.jp・Cloudflare のログイン）の証明でも、今までのパスワードでも入れること（2026-10-14）。外部APIなし。

受け入れ基準:
- 正しい証明（署名・発行元・宛先・期限）があれば、パスワードなしで画面と API に入れる
- 宛先違い・発行元違い・期限切れ・別の鍵・作られた証明は「証明なし」と同じ（画面はログインへ・API は JSON 401）
- 設定（CF_ACCESS_AUD）が無ければ何も変わらない
- 今までのパスワードのログインはそのまま使える
"""
import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as appmod  # noqa: E402
import cloudflare_access as ca  # noqa: E402

TEAM = "https://apprendre.cloudflareaccess.com"
AUD = "aud-for-studios"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(aud=AUD, iss=TEAM, exp=None, key=KEY):
    now = int(time.time())
    return jwt.encode({"email": "someone@example.com", "aud": [aud], "iss": iss, "iat": now,
                       "exp": exp if exp is not None else now + 600}, key, algorithm="RS256")


class _Keys:
    def get_signing_key_from_jwt(self, _token):
        return type("K", (), {"key": KEY.public_key()})()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(appmod, "APP_PASSWORD", "testpass")
    monkeypatch.setenv("CF_ACCESS_AUD", AUD)
    monkeypatch.setattr(ca, "_jwks", lambda team: _Keys())
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def test_valid_proof_logs_in_without_password(client):
    res = client.get("/", headers={ca.HEADER: token()})
    assert res.status_code == 200
    with client.session_transaction() as session:
        assert session.get("authenticated") is True
    assert client.get("/api/key-attribution").status_code == 200


@pytest.mark.parametrize("bad", [lambda: token(aud="another-app"), lambda: token(iss="https://other.cloudflareaccess.com"),
                                 lambda: token(exp=int(time.time()) - 3600), lambda: token(key=OTHER), lambda: "forged"])
def test_wrong_proof_is_treated_as_no_proof(client, bad):
    assert client.get("/", headers={ca.HEADER: bad()}).status_code == 302
    res = client.get("/api/key-attribution", headers={ca.HEADER: bad()})
    assert res.status_code == 401 and res.is_json


def test_old_password_still_works(client):
    assert client.get("/").status_code == 302
    client.post("/login", data={"password": "testpass"})
    assert client.get("/").status_code == 200


def test_without_the_setting_nothing_changes(monkeypatch):
    monkeypatch.setattr(appmod, "APP_PASSWORD", "testpass")
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    monkeypatch.setattr(ca, "_jwks", lambda team: _Keys())
    assert appmod.app.test_client().get("/", headers={ca.HEADER: token()}).status_code == 302
