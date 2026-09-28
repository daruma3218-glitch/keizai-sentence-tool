"""引用で使う写真（quote_searcher.py・2026-09-28 社長「Commons だけだと範囲が少ない。引用で使う前提で広げたら」）。"""
import io

from PIL import Image

import quote_searcher as qs
from commons_searcher import build_credits_text


def _jpeg(w=800, h=450):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 90, 60)).save(buf, format="JPEG")
    return buf.getvalue()


SEL = {"no": 8, "query": "ドトールコーヒー 原宿 1号店", "topic": "ドトール1号店",
       "sentence": "1980年4月18日、東京・原宿に、立ち飲みスタイルの1号店を開きます。"}


def _search(pages):
    return lambda sel: {"source_url": pages[0][0], "source_title": pages[0][1],
                        "all_urls": [{"url": u, "title": t} for u, t in pages[1:]]}


def test_stock_sns_shop_and_blog_pages_are_not_used():
    for url in ("https://www.gettyimages.co.jp/x", "https://pixta.jp/x", "https://x.com/doutor/status/1",
                "https://www.amazon.co.jp/dp/1", "https://ameblo.jp/someone/entry-1.html", "https://note.com/a/n/b"):
        assert qs.is_blocked(url), url
    for url in ("https://www.doutor.co.jp/about/history/", "https://ja.wikipedia.org/wiki/ドトールコーヒー",
                "https://www.nikkei.com/article/1"):
        assert not qs.is_blocked(url), url


def test_first_official_photo_that_shows_the_subject_is_quoted(monkeypatch):
    monkeypatch.setattr(qs, "_page_image", lambda url: url + "/og.jpg")
    sizes = {"https://www.doutor.co.jp/about/history//og.jpg": _jpeg(200, 100),   # 小さすぎる
             "https://www.nikkei.com/article/1/og.jpg": _jpeg()}
    monkeypatch.setattr(qs, "_fetch", lambda url, timeout=15: sizes.get(url, _jpeg()))
    judged = []

    def judge(sel, cand, data):
        judged.append(cand["thumb_url"])
        return (True, "ドトールの店舗") if "nikkei" in cand["thumb_url"] else (False, "ロゴ")

    info = qs.find_quote_photo(None, SEL, search=_search([
        ("https://pixta.jp/photo/1", "素材"),
        ("https://www.doutor.co.jp/about/history/", "沿革"),
        ("https://www.nikkei.com/article/1", "ドトール 原宿の1号店"),
    ]), judge=judge)
    assert info["license"] == "引用" and info["attribution"] == "www.nikkei.com"
    assert info["source_url"] == "https://www.nikkei.com/article/1"
    assert judged == ["https://www.nikkei.com/article/1/og.jpg"]  # 素材サイトと小さい画像は判定にも回さない


def test_nothing_is_quoted_when_no_image_shows_the_subject(monkeypatch):
    monkeypatch.setattr(qs, "_page_image", lambda url: url + "/og.jpg")
    monkeypatch.setattr(qs, "_fetch", lambda url, timeout=15: _jpeg())
    info = qs.find_quote_photo(None, SEL, search=_search([("https://www.doutor.co.jp/", "公式")]),
                               judge=lambda sel, cand, data: (False, "ロゴ"))
    assert info == {}


def test_credits_list_quoted_sources_for_on_screen_credit():
    text = build_credits_text([{"no": 8, "license": "引用", "source_url": "https://www.nikkei.com/article/1",
                                "source_title": "ドトール 原宿の1号店", "attribution": "www.nikkei.com"}])
    assert "引用した画像の出典" in text and "https://www.nikkei.com/article/1" in text and "№8" in text


def test_single_redo_falls_back_to_quotes(tmp_path, monkeypatch):
    import app as appmod
    import commons_searcher as cs
    import web_searcher
    (tmp_path / "images").mkdir()
    monkeypatch.setattr(cs, "run_commons_search_for_selections", lambda client, sels, **k: {})
    monkeypatch.setattr(cs, "suggest_query", lambda client, s, c="": {})
    monkeypatch.setattr(qs, "run_quote_search_for_selections", lambda client, sels, **k: {8: {
        "no": 8, "thumb_url": "http://x/og.jpg", "source_url": "https://www.nikkei.com/article/1",
        "source_title": "t", "topic": "t", "license": "引用", "attribution": "www.nikkei.com",
        "commons_page_url": "", "source_type": "引用・article"}})
    monkeypatch.setattr(web_searcher, "download_thumbnail", lambda url, path: True)
    captured = {}
    monkeypatch.setattr(appmod, "_update_regen_snapshot", lambda *a, **k: captured.update(k))
    with appmod.app.test_request_context():
        res = appmod._regenerate_web_photo(tmp_path, 8, {"sentence": "原宿に1号店を開きます。"},
                                           {"anthropic": ""}, {"photo_source": "commons_then_quote"})
    assert res.get_json()["ok"] is True
    assert captured["extra"]["license"] == "引用" and captured["extra"]["web_source_type"] == "引用・article"
