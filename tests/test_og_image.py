#!/usr/bin/env python3
"""Web検索の og:image 取得（改修 #6）の pytest。

受け入れ基準:
- og:image / twitter:image を属性順どちらのHTMLからも抽出できる
- 相対URL・プロトコル相対URLを絶対化する
- 無ければ空文字（例外を出さない）
- _page_main_image は HTML 以外の Content-Type では空文字
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web_searcher  # noqa: E402


def test_extract_og_image_property_first():
    html = '<html><head><meta property="og:image" content="https://ex.com/a.jpg"></head></html>'
    assert web_searcher._extract_og_image(html) == "https://ex.com/a.jpg"


def test_extract_og_image_content_first():
    # 属性順が逆（content が先）のHTML
    html = '<meta content="https://ex.com/b.png" property="og:image">'
    assert web_searcher._extract_og_image(html) == "https://ex.com/b.png"


def test_extract_twitter_image_fallback():
    html = '<meta name="twitter:image" content="https://ex.com/t.jpg">'
    assert web_searcher._extract_og_image(html) == "https://ex.com/t.jpg"


def test_extract_relative_and_protocol_relative():
    assert web_searcher._extract_og_image(
        '<meta property="og:image" content="/img/c.jpg">', base_url="https://ex.com/news/1"
    ) == "https://ex.com/img/c.jpg"
    assert web_searcher._extract_og_image(
        '<meta property="og:image" content="//cdn.ex.com/d.jpg">'
    ) == "https://cdn.ex.com/d.jpg"


def test_extract_none():
    assert web_searcher._extract_og_image("<html><body>no meta</body></html>") == ""
    assert web_searcher._extract_og_image("") == ""


class _FakeResp:
    def __init__(self, body: bytes, ctype: str):
        self._body = body
        self.headers = {"Content-Type": ctype}

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_page_main_image_fetches_og(monkeypatch):
    html = b'<html><head><meta property="og:image" content="https://ex.com/main.jpg"></head></html>'
    monkeypatch.setattr(web_searcher.urllib.request, "urlopen",
                        lambda req, timeout=8: _FakeResp(html, "text/html; charset=utf-8"))
    assert web_searcher._page_main_image("https://ex.com/article") == "https://ex.com/main.jpg"


def test_page_main_image_non_html(monkeypatch):
    monkeypatch.setattr(web_searcher.urllib.request, "urlopen",
                        lambda req, timeout=8: _FakeResp(b"%PDF-1.4", "application/pdf"))
    assert web_searcher._page_main_image("https://ex.com/file.pdf") == ""


def test_page_main_image_bad_scheme():
    assert web_searcher._page_main_image("javascript:alert(1)") == ""
    assert web_searcher._page_main_image("") == ""
