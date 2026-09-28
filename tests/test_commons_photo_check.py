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
