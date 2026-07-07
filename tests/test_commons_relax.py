#!/usr/bin/env python3
"""Commons 検索のサイズ条件パラメータ化（改修 #9）の pytest。

受け入れ基準:
- 既定(400x300)では小さい画像(300x200)は不採用（従来挙動を維持）
- min_w/min_h を緩めると同じ画像が採用される（0件トピックの救済）
"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commons_searcher  # noqa: E402


def _api_response(width, height):
    return {
        "query": {"pages": {"1": {
            "index": 1,
            "title": "File:Small_historic_photo.jpg",
            "imageinfo": [{
                "url": "https://upload.wikimedia.org/x/Small_historic_photo.jpg",
                "thumburl": "https://upload.wikimedia.org/x/thumb.jpg",
                "descriptionurl": "https://commons.wikimedia.org/wiki/File:Small_historic_photo.jpg",
                "mime": "image/jpeg",
                "width": width, "height": height,
                "extmetadata": {
                    "LicenseShortName": {"value": "CC BY-SA 4.0"},
                    "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"},
                    "Artist": {"value": "Someone"},
                },
            }],
        }}}
    }


class _FakeResp:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_default_rejects_small_but_relaxed_accepts(monkeypatch):
    payload = _api_response(300, 200)  # 既定(400x300)未満・緩和(240x160)以上
    monkeypatch.setattr(commons_searcher.urllib.request, "urlopen",
                        lambda req, timeout=20: _FakeResp(payload))

    assert commons_searcher.search_commons_one("歴史 写真") is None  # 既定では不採用
    res = commons_searcher.search_commons_one("歴史 写真", min_w=240, min_h=160)
    assert res is not None and res["license"].startswith("CC BY")


def test_relaxed_still_rejects_tiny(monkeypatch):
    payload = _api_response(100, 80)  # ロゴ級は緩和後も不採用
    monkeypatch.setattr(commons_searcher.urllib.request, "urlopen",
                        lambda req, timeout=20: _FakeResp(payload))
    assert commons_searcher.search_commons_one("ロゴ", min_w=240, min_h=160) is None
