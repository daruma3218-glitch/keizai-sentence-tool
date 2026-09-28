"""Commons の写真は写っているものを確かめてから使う（2026-09-28）。

社長の試験（ルノアール回 №46「喫茶店業界を大きく変えたのがドトールでした」）で、19世紀の本の表紙の
スキャン（DjVu）が採用された。検索が本の中の「Doutor」（ポルトガル語で博士）に当たっていた。
"""
from types import SimpleNamespace
from unittest import mock

import commons_searcher as cs
from test_commons import _fake_urlopen, _page


def _pages(*pages):
    return {"query": {"pages": {str(i): p for i, p in enumerate(pages, 1)}}}


def test_book_scans_are_not_photos():
    payload = _pages(_page(1, "Camoens_-_his_life_and_his_Lusiads_Volume_2.djvu", "Public domain",
                           mime="image/vnd.djvu"),
                     _page(2, "Doutor_Harajuku.jpg", "CC BY-SA 4.0"))
    with mock.patch.object(cs.urllib.request, "urlopen", return_value=_fake_urlopen(payload)) as op:
        found = cs.search_commons_candidates("ドトール")
    assert [c["title"] for c in found] == ["Doutor_Harajuku.jpg"]
    assert "filetype%3Abitmap" in op.call_args[0][0].full_url  # 検索も写真だけに絞る


class _Judge:
    """1枚目は別物、2枚目はその店、と答える判定役（写真の取得は偽物）。"""

    def __init__(self, answers):
        self.answers = list(answers)
        self.seen = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.seen.append(kw["messages"][0]["content"][1]["text"])
        return SimpleNamespace(content=[SimpleNamespace(text=self.answers.pop(0))])


def _run(monkeypatch, answers, pages):
    judge = _Judge(answers)
    monkeypatch.setattr(cs, "search_commons_candidates",
                        lambda q, *a, **k: [{"title": t, "thumb_url": f"http://x/{t}", "url": "",
                                             "license": "CC BY 4.0", "license_url": "", "attribution": "",
                                             "commons_page_url": f"http://commons/{t}"} for t in pages])
    monkeypatch.setattr(cs, "photo_shows_topic", cs.photo_shows_topic)
    import verifier
    monkeypatch.setattr(verifier, "_encode_for_review", lambda data, suffix, max_side=0: ("eA==", "image/jpeg"))

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"jpeg"

    monkeypatch.setattr(cs.urllib.request, "urlopen", lambda req, timeout=0: _Resp())
    monkeypatch.setattr(cs, "_translate_queries", lambda client, queries, log=None: {})
    logs = []
    sel = {"no": 46, "query": "ドトール 1号店", "topic": "ドトール",
           "sentence": "さて、こうした喫茶店業界を、大きく変えたのがドトールでした。"}
    got = cs.run_commons_search_for_selections(judge, [sel], max_workers=1,
                                              log=lambda *a: logs.append(" ".join(map(str, a))))
    return got, judge, logs


def test_only_a_photo_showing_the_subject_is_used(monkeypatch):
    got, judge, logs = _run(monkeypatch, ['{"match": false, "what": "本の表紙"}',
                                          '{"match": true, "what": "ドトールの店舗"}'],
                            ["Book.jpg", "Doutor_shop.jpg"])
    assert got[46]["source_title"] == "Doutor_shop.jpg"
    assert "喫茶店業界を、大きく変えたのがドトール" in judge.seen[0]  # 文を見せて判定する
    assert any("Book.jpg" in l and "本の表紙" in l for l in logs)


def test_no_matching_photo_means_no_web_photo(monkeypatch):
    got, _, _ = _run(monkeypatch, ['{"match": false, "what": "本"}', "読めない答え"], ["A.jpg", "B.jpg"])
    assert 46 not in got  # 当てはまる写真が無ければ採用しない（世界観のイラストで代わりを作る）


def test_long_router_words_are_shortened_until_a_photo_is_found(monkeypatch):
    # 9/28 試験 20260928_010838: ルーターの検索語が長く、写真だけに絞ると 0/3 件だった
    assert cs.shorter_queries("Doutor Coffee Harajuku first store") == [
        "Doutor Coffee Harajuku first", "Doutor Coffee Harajuku", "Doutor Coffee", "Doutor"]
    asked = []

    def fake_candidates(q, *a, **k):
        asked.append(q)
        if q != "Doutor Coffee Harajuku":
            return []
        return [{"title": "Doutor_Harajuku.jpg", "thumb_url": "", "url": "", "license": "CC BY-SA 4.0",
                 "license_url": "", "attribution": "", "commons_page_url": ""}]

    monkeypatch.setattr(cs, "search_commons_candidates", fake_candidates)
    monkeypatch.setattr(cs, "photo_shows_topic", lambda client, sel, cand: (True, "ドトール原宿店"))
    monkeypatch.setattr(cs, "_translate_queries", lambda client, queries, log=None: {
        "ドトールコーヒー 原宿 1号店": "Doutor Coffee Harajuku first store"})
    got = cs.run_commons_search_for_selections(
        object(), [{"no": 8, "query": "ドトールコーヒー 原宿 1号店", "topic": "ドトール1号店"}], max_workers=1)
    assert got[8]["source_title"] == "Doutor_Harajuku.jpg"
    assert asked[:3] == ["ドトールコーヒー 原宿 1号店", "Doutor Coffee Harajuku first store",
                         "Doutor Coffee Harajuku first store"]


def test_single_web_photo_redo_uses_commons_for_commons_channels(tmp_path, monkeypatch):
    # 1枚の「Web写真で作り直す」も、写真を Commons に限るチャンネルでは Commons から確かめて使う
    import app as appmod
    import web_searcher
    (tmp_path / "images").mkdir()
    used = {}

    def fake_commons(client, selections, max_workers=1, **kw):
        used["sel"] = selections[0]
        return {8: {"no": 8, "thumb_url": "http://x/t.jpg", "source_url": "http://commons/f", "source_title": "f.jpg",
                    "topic": "t", "license": "CC BY-SA 4.0", "license_url": "", "attribution": "Mc681",
                    "commons_page_url": "http://commons/f"}}

    monkeypatch.setattr(cs, "run_commons_search_for_selections", fake_commons)
    monkeypatch.setattr(web_searcher, "run_web_search_for_selections",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("一般の Web 検索は使わない")))
    monkeypatch.setattr(web_searcher, "download_thumbnail", lambda url, path: path.write_bytes(b"jpg") or True)
    captured = {}
    monkeypatch.setattr(appmod, "_update_regen_snapshot", lambda *a, **k: captured.update(k))
    with appmod.app.test_request_context():
        res = appmod._regenerate_web_photo(tmp_path, 8, {"sentence": "1980年、原宿に1号店を開きます。"},
                                           {"anthropic": ""}, {"photo_source": "commons"})
    assert res.get_json()["ok"] is True
    assert used["sel"]["sentence"].startswith("1980年")
    assert captured["extra"]["license"] == "CC BY-SA 4.0" and captured["extra"]["attribution"] == "Mc681"


def test_single_redo_uses_saved_words_or_asks_for_them(tmp_path, monkeypatch):
    # 9/28: 文に店名が無い（「原宿に1号店を開きます」）と、文をそのまま検索しても見つからなかった
    import app as appmod
    import web_searcher
    (tmp_path / "images").mkdir()
    seen = []
    monkeypatch.setattr(cs, "run_commons_search_for_selections",
                        lambda client, sels, **k: seen.append(dict(sels[0])) or {})
    monkeypatch.setattr(cs, "suggest_query", lambda client, sentence, context="": {
        "query": "Doutor Coffee Harajuku first store", "topic": "ドトール1号店"})
    monkeypatch.setattr(web_searcher, "download_thumbnail", lambda url, path: True)
    with appmod.app.test_request_context():
        appmod._regenerate_web_photo(tmp_path, 8, {"sentence": "原宿に1号店を開きます。",
                                                  "web_query": "ドトールコーヒー 原宿 1号店",
                                                  "web_query_topic": "ドトール1号店"},
                                     {"anthropic": ""}, {"photo_source": "commons"})
        appmod._regenerate_web_photo(tmp_path, 8, {"sentence": "原宿に1号店を開きます。",
                                                  "block_text": "喫茶店業界を変えたのがドトールでした。"},
                                     {"anthropic": ""}, {"photo_source": "commons"})
    assert seen[0]["query"] == "ドトールコーヒー 原宿 1号店"   # 自動の回で使った検索語
    assert seen[1]["query"] == "Doutor Coffee Harajuku first store"  # 文と段落から決めた検索語
