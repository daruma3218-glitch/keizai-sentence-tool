#!/usr/bin/env python3
"""制作資料パック（成功の法則向け改修）の pytest。

受け入れ基準:
- source_collector: 章ごとの元ネタ動画をJSONで取り出し、URL形式外は除外、
  検索結果に実在したURLに verified を付け、per_chapter で頭打ち
- collect_source_videos: 章ごとに先頭文ダイジェストを渡し {chapter_index: [...]} で返す
- pipeline._write_source_pack: sources.md / sources.html に
  章タイトル・動画・一次資料・採用写真・差し替え候補が入る
- material_type: 選定プロンプトに素材タイプ指示が入り、検索クエリにヒントが反映され、
  結果 info に material_type が残る
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline as plmod  # noqa: E402
import source_collector  # noqa: E402
import web_searcher  # noqa: E402


# ---------- source_collector ----------

def test_video_prompt_requests_overseas_primary_sources(monkeypatch):
    """元ネタ動画の検索指示に、英語（原語）検索・TED等の海外一次情報の優先が入る。"""
    captured = {}

    def fake_research(client, query, system, **kw):
        captured["query"] = query
        captured["max_uses"] = kw.get("max_uses")
        return "[]", []
    monkeypatch.setattr(source_collector, "_claude_research_call", fake_research)
    source_collector.collect_chapter_source_videos(None, "T", "第1章", "要約", per_chapter=3)
    q = captured["query"]
    assert "英語でも検索" in q and "TED" in q and "原語" in q
    assert captured["max_uses"] >= 4  # 日英両方で検索できる回数


def test_selection_prompt_allows_english_queries(monkeypatch):
    """primary_media の選定プロンプトで英語クエリが許可されている。"""
    captured = {}

    class _FakeMessages:
        def create(self, **kw):
            captured["kw"] = kw
            return _FakeMsgResp("[]")

    class _FakeClient:
        messages = _FakeMessages()

    web_searcher._select_chunk(
        _FakeClient(), [{"no": 1, "sentence": "海外の起業家についての文。" * 3}],
        target_count=1, exclude_nos=set(), log=lambda *a, **k: None, profile="primary_media")
    content = captured["kw"]["messages"][0]["content"]
    text = "\n".join(b.get("text", "") for b in content) if isinstance(content, list) else content
    assert "英語で作ってよい" in text and "日本語または英語" in text


def test_collect_chapter_source_videos_filters_and_verifies(monkeypatch):
    text = json.dumps([
        {"url": "https://www.youtube.com/watch?v=abc", "title": "本人講演", "reason": "本人の一次情報"},
        {"url": "https://ted.com/talks/xyz", "title": "TED", "reason": "元ネタ"},
        {"url": "ftp://bad.example/vid", "title": "不正スキーム", "reason": "x"},
        {"url": "https://youtu.be/hallucinated", "title": "幻覚URL", "reason": "x"},
        {"url": "https://vimeo.com/4", "title": "4本目", "reason": "上限外"},
    ], ensure_ascii=False)
    real = [{"url": "https://www.youtube.com/watch?v=abc", "title": "本人講演"},
            {"url": "https://ted.com/talks/xyz", "title": "TED"}]
    monkeypatch.setattr(source_collector, "_claude_research_call",
                        lambda client, q, s, **kw: (text, real))
    out = source_collector.collect_chapter_source_videos(None, "T", "第1章", "要約", per_chapter=3)
    assert len(out) == 3  # 不正スキームは除外し、上限3で頭打ち
    assert out[0]["verified"] is True and out[1]["verified"] is True
    assert out[2]["url"] == "https://youtu.be/hallucinated" and out[2]["verified"] is False


def test_collect_source_videos_digest_per_chapter(monkeypatch):
    seen = {}

    def fake_one(client, title, ch_title, digest, per_chapter=3, timeout=75.0):
        seen[ch_title] = digest
        return [{"url": "https://y/1", "title": ch_title, "reason": "r", "verified": True}]
    monkeypatch.setattr(source_collector, "collect_chapter_source_videos", fake_one)

    chapters = [{"title": "序章"}, {"title": "本論"}]
    rows = [
        {"no": 1, "chapter_index": 0, "sentence": "最初の文。"},
        {"no": 2, "chapter_index": 1, "sentence": "本論の文その1。"},
        {"no": 3, "chapter_index": 1, "sentence": "本論の文その2。"},
    ]
    res = source_collector.collect_source_videos(None, "T", chapters, rows, per_chapter=3)
    assert set(res.keys()) == {0, 1}
    assert "最初の文。" in seen["序章"]
    assert "本論の文その1。" in seen["本論"]


# ---------- _write_source_pack ----------

def test_write_source_pack_outputs(tmp_path):
    pipe = plmod.SentencePipeline(manuscript_text="x" * 200, output_dir=tmp_path / "job")
    chapters = [{"title": "第1章 習慣の力"}]
    rows = [{
        "no": 12, "chapter_index": 0, "sentence": "アトミック・ハビッツの著者は毎日1%の改善を説いた。",
        "web_source_url": "https://example.com/atomic", "web_source_title": "著者インタビュー",
        "web_local_file": "12.jpg", "web_material_type": "person",
        "web_candidates": [
            {"url": "https://example.com/atomic", "title": "著者インタビュー"},
            {"url": "https://example.com/alt", "title": "別記事"},
        ],
    }]
    videos = {0: [{"url": "https://youtube.com/watch?v=k", "title": "本人講演",
                   "reason": "原典スピーチ", "verified": True}]}
    infos = [{"no": 12, "source_url": "https://example.com/atomic"}]
    pipe._write_source_pack("成功の法則テスト", chapters, rows, videos, infos)

    md = (tmp_path / "job" / "sources.md").read_text(encoding="utf-8")
    html = (tmp_path / "job" / "sources.html").read_text(encoding="utf-8")
    for s in ("第1章 習慣の力", "https://youtube.com/watch?v=k", "原典スピーチ",
              "№12", "https://example.com/atomic", "12.jpg", "差し替え候補"):
        assert s in md, f"md に {s} が無い"
    assert "images/12.jpg" in html and "本人講演" in html
    assert "https://example.com/alt" in md  # 未採用候補も控えとして載る


# ---------- material_type ----------

class _FakeMsgResp:
    class _B:
        def __init__(self, t):
            self.text = t
    def __init__(self, text):
        self.content = [self._B(text)]
        self.usage = None
        self.stop_reason = "end_turn"


def test_select_chunk_primary_media_asks_material_type(monkeypatch):
    captured = {}

    class _FakeMessages:
        def create(self, **kw):
            captured["kw"] = kw
            return _FakeMsgResp('[{"no": 1, "topic": "t", "query": "q", "material_type": "scene"}]')

    class _FakeClient:
        messages = _FakeMessages()

    out = web_searcher._select_chunk(
        _FakeClient(), [{"no": 1, "sentence": "実際の工場の現場で改善が行われた。" * 3}],
        target_count=1, exclude_nos=set(), log=lambda *a, **k: None, profile="primary_media")
    content = captured["kw"]["messages"][0]["content"]
    text = "\n".join(b.get("text", "") for b in content) if isinstance(content, list) else content
    assert "material_type" in text and "素材タイプ" in text
    assert out and out[0].get("material_type") == "scene"  # 解析結果に残る


def test_search_single_sentence_uses_material_hint(monkeypatch):
    captured = {}

    def fake_research(client, query, system, **kw):
        captured["query"] = query
        return "", [{"url": "https://example.com/scene-report", "title": "現場レポート"}]
    monkeypatch.setattr(web_searcher, "_claude_research_call", fake_research)
    monkeypatch.setattr(web_searcher, "_page_main_image", lambda u, timeout=8: "https://img/x.jpg")

    info = web_searcher.search_single_sentence(
        None, {"no": 5, "query": "工場 現場", "topic": "工場改善", "material_type": "scene"},
        profile="primary_media")
    assert "素材タイプ [scene]" in captured["query"]
    assert info["material_type"] == "scene"
    assert info["thumb_url"] == "https://img/x.jpg"
