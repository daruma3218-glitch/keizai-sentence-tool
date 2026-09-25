#!/usr/bin/env python3
"""投入画面の簡素化（2026-09-25 社長「不要な初期設定もUIとして使いやすくしよう」）の pytest。

受け入れ基準:
- 画像キーのないチャンネル（例: 共通）は選択肢に出さない（キー未設定の警告から始まらない）
- 全チャンネルにキーが無い環境（手元の確認など）では全部出す
- 選択肢の名前にキーの状態を付けない。設定の要約を出し、細かい設定は「詳しい設定」に畳む
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as appmod  # noqa: E402


def _page(monkeypatch, keys_for):
    monkeypatch.setattr(appmod, "APP_PASSWORD", "")
    monkeypatch.setattr(appmod, "resolve_channel_keys", lambda c: keys_for(c["id"]))
    return appmod.app.test_client().get("/").get_data(as_text=True)


def test_channels_without_image_keys_are_hidden(monkeypatch):
    html = _page(monkeypatch, lambda cid: {"gemini": "" if cid == "default" else "g", "openai": "", "anthropic": ""})
    assert 'value="keizai"' in html and 'value="seikou"' in html
    assert 'value="default"' not in html
    assert "（専用キー:" not in html and "（共通キー:" not in html  # 名前にキーの状態を付けない


def test_all_channels_are_listed_when_no_channel_has_keys(monkeypatch):
    html = _page(monkeypatch, lambda cid: {"gemini": "", "openai": "", "anthropic": ""})
    assert 'value="default"' in html and 'value="keizai"' in html
    assert 'id="keyWarning"' in html


def test_start_screen_summarizes_and_folds_detailed_settings(monkeypatch):
    html = _page(monkeypatch, lambda cid: {"gemini": "g", "openai": "o", "anthropic": ""})
    assert 'id="settingsSummaryList"' in html
    assert '<details id="advancedSettings"' in html
    # 細かい設定は畳んだ中にあり、送信される項目名は変わらない
    folded = html.split('<details id="advancedSettings"', 1)[1].split("</details>", 1)[0]
    for name in ('name="provider"', 'name="openai_model"', 'name="worldview_desc"', 'name="route_mode"',
                 'name="concurrency"', 'name="max_diagrams"', 'name="web_image_count"', 'name="verify_diagrams"'):
        assert name in folded, name
    assert "① チャンネルを選ぶ" in html and "② 原稿を入れる" in html and "③ 図解をまとめて作る" in html
