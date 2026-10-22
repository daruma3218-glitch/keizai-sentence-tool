"""ハブの案件の Trello カードから原稿を読む（2026-10-22）。中継は偽物に差し替え、外へは出ない。"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as appmod  # noqa: E402
import material_store  # noqa: E402
import trello_source  # noqa: E402

DOC = "https://docs.google.com/document/d/abc123/edit"


def test_one_doc_is_read_through_and_card_scope_is_kept():
    card = {"title": "カード", "text": "説明", "content_kind": "", "choices": [{"label": "原稿", "url": DOC}]}
    doc = {"title": "原稿", "text": "本文", "content_kind": "manuscript", "provider": "Googleドキュメント",
           "choices": [{"label": "原稿", "url": DOC}, {"label": "私的", "url": "https://example.test/x"}]}
    with patch.object(trello_source, "_request", side_effect=[card, doc]) as read:
        result = trello_source.load("russia", "member@example.test", "https://trello.com/c/Ab3dE6gH")
    assert result["manuscript"] and result["text"] == "本文"
    assert [c["url"] for c in result["choices"]] == [DOC]  # Googleドキュメント以外の住所は返さない
    assert read.call_args_list[0].args == ("roshia", "member@example.test")
    assert read.call_args_list[0].kwargs == {"source_id": "trello:Ab3dE6gH", "resource": ""}
    assert read.call_args_list[1].kwargs["resource"] == DOC


def test_several_docs_are_offered_not_guessed():
    card = {"title": "カード", "content_kind": "", "choices": [{"label": "初稿", "url": DOC}, {"label": "改稿", "url": DOC + "2"}]}
    with patch.object(trello_source, "_request", return_value=card) as read:
        result = trello_source.load("economy", "member@example.test", "https://trello.com/c/Ab3dE6gH")
    assert read.call_count == 1 and not result["manuscript"] and len(result["choices"]) == 2


@pytest.mark.parametrize("channel,email,url", [("russia", "", "https://trello.com/c/Ab3dE6gH"),
                                               ("edo", "m@example.test", "https://trello.com/c/Ab3dE6gH"),
                                               ("russia", "m@example.test", "https://evil.test/c/Ab3dE6gH")])
def test_refuses_without_login_channel_or_card(channel, email, url):
    with patch.object(trello_source, "_request") as read, pytest.raises(trello_source.SourceError):
        trello_source.load(channel, email, url)
    read.assert_not_called()


def test_route_reads_only_the_projects_own_card(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "APP_PASSWORD", "testpass")
    monkeypatch.setattr(appmod, "OUTPUT_ROOT", tmp_path)
    project = material_store.create_project(tmp_path, "russia", "ロシアの回", {}, "https://trello.com/c/Ab3dE6gH")
    client = appmod.app.test_client()
    with client.session_transaction() as s:
        s["authenticated"] = True
    with patch("cloudflare_access.verified_email", return_value="member@example.test"), \
         patch.object(trello_source, "_request", return_value={"title": "原稿", "text": "本文", "content_kind": "manuscript", "choices": []}) as read:
        data = client.get(f"/api/material-projects/{project['project_id']}/trello-source").get_json()
    assert data["ok"] and data["text"] == "本文"
    assert read.call_args.kwargs["source_id"] == "trello:Ab3dE6gH"
    assert client.get("/api/material-projects/..%2Fx/trello-source").status_code in (404, 409)
