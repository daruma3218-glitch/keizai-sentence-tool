#!/usr/bin/env python3
"""allocator.py（v3 Step4）の pytest（LLM不使用・決定論）。

受け入れ基準:
- 250 文で beat_id / est_start / display が全行に付く
- 画像配分が max_diagrams を超えない
- beat_mode=False で v2 互換（display を付けない）
- importance が高いビートが優先される
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from allocator import allocate  # noqa: E402
from pipeline import SentencePipeline  # noqa: E402

_ROUTES = ["chart", "map", "diagram", "illustration", "web_photo", "skip"]


def _make(n=250):
    rows, routes = [], {}
    for i in range(1, n + 1):
        rows.append({"no": i, "sentence": "これはテスト用のセンテンスです。" * ((i % 3) + 1)})
        routes[i] = {
            "route": _ROUTES[i % len(_ROUTES)],
            "importance": (i % 5) + 1,
            "beat": "new" if i % 4 == 0 else "continue",
            "entities": [],
        }
    return rows, routes


def test_all_rows_get_fields_250():
    rows, routes = _make(250)
    a = allocate(rows, routes, max_diagrams=100, chars_per_sec=5.5, beat_mode=True)
    assert len(a) == 250
    for r in rows:
        info = a[r["no"]]
        assert ":" in info["est_start"], "est_start が mm:ss でない"
        if routes[r["no"]]["route"] == "skip":
            assert info["display"] == "none" and info["beat_id"] is None
        else:
            assert info["beat_id"] is not None, "非skip文に beat_id が無い"
            assert info["display"] in ("image", "hold", "none"), "display が不正"


def test_budget_not_exceeded():
    rows, routes = _make(250)
    for mx in (5, 20, 50, 100):
        a = allocate(rows, routes, max_diagrams=mx, beat_mode=True)
        imgs = sum(1 for v in a.values() if v["display"] == "image")
        assert imgs <= mx, f"配分 {imgs} 枚 > 上限 {mx} 枚"


def test_beat_mode_false_v2_compat():
    rows, routes = _make(40)
    a = allocate(rows, routes, max_diagrams=20, beat_mode=False)
    # v2 互換: display は付けない（none のまま）。beat_id / est_start は付く。
    for r in rows:
        info = a[r["no"]]
        assert info["display"] == "none"
        assert ":" in info["est_start"]
        if routes[r["no"]]["route"] != "skip":
            assert info["beat_id"] is not None


def test_high_importance_prioritized():
    # 予算1。importance5 のビートと importance1 のビート → 5 が画像になる。
    rows = [
        {"no": 1, "sentence": "重要な核心の主張です。"},
        {"no": 2, "sentence": "ただの繋ぎの文。"},
    ]
    routes = {
        1: {"route": "diagram", "importance": 5, "beat": "new", "entities": []},
        2: {"route": "diagram", "importance": 1, "beat": "new", "entities": []},
    }
    a = allocate(rows, routes, max_diagrams=1, beat_mode=True)
    assert a[1]["display"] == "image"
    assert a[2]["display"] == "hold"


def test_timecode_accumulates():
    rows = [{"no": 1, "sentence": "あ" * 55}, {"no": 2, "sentence": "い" * 55}]
    routes = {1: {"route": "diagram", "importance": 3, "beat": "new"},
              2: {"route": "diagram", "importance": 3, "beat": "continue"}}
    a = allocate(rows, routes, max_diagrams=5, chars_per_sec=5.5, beat_mode=True)
    assert a[1]["est_start"] == "00:00"
    assert a[2]["est_start"] == "00:10"  # 55/5.5 = 10秒


def test_continuing_route_does_not_merge_chapters():
    # 本番の2章試験で、後の文だけが画像になり、先頭がholdになった回帰。
    rows = [
        {"no": 1, "chapter_index": 0, "sentence": "工場から店へ商品を運びます。"},
        {"no": 2, "chapter_index": 1, "sentence": "商品を受け取ると在庫が増えます。"},
    ]
    routes = {
        1: {"route": "diagram", "importance": 3, "beat": "new"},
        2: {"route": "diagram", "importance": 4, "beat": "continue"},
    }
    a = allocate(rows, routes, max_diagrams=2)
    assert a[1]["beat_id"] != a[2]["beat_id"]
    assert [a[n]["display"] for n in (1, 2)] == ["image", "image"]


def test_leading_lower_importance_row_cannot_hold_future_image():
    rows = [{"no": 1, "sentence": "導入です。"}, {"no": 2, "sentence": "核心です。"}]
    routes = {
        1: {"route": "diagram", "importance": 2, "beat": "new"},
        2: {"route": "diagram", "importance": 5, "beat": "continue"},
    }
    a = allocate(rows, routes, max_diagrams=1)
    assert [a[n]["display"] for n in (1, 2)] == ["none", "image"]


def test_unfunded_chapter_cannot_hold_previous_chapter_image():
    rows = [
        {"no": 1, "chapter_index": 0, "sentence": "重要な図です。"},
        {"no": 2, "chapter_index": 1, "sentence": "別の章です。"},
    ]
    routes = {
        1: {"route": "diagram", "importance": 5, "beat": "new"},
        2: {"route": "diagram", "importance": 2, "beat": "continue"},
    }
    a = allocate(rows, routes, max_diagrams=1)
    assert [a[n]["display"] for n in (1, 2)] == ["image", "none"]


def test_skip_breaks_the_image_continuation():
    rows = [{"no": n, "sentence": "試験文です。"} for n in (1, 2, 3)]
    routes = {
        1: {"route": "diagram", "importance": 5, "beat": "new"},
        2: {"route": "skip", "importance": 1, "beat": "continue"},
        3: {"route": "diagram", "importance": 2, "beat": "continue"},
    }
    a = allocate(rows, routes, max_diagrams=1)
    assert [a[n]["display"] for n in (1, 2, 3)] == ["image", "none", "none"]
    assert a[1]["beat_id"] != a[3]["beat_id"]


def test_every_hold_has_an_earlier_image_in_same_unbroken_chapter():
    rows, routes = _make(250)
    for row in rows:
        row["chapter_index"] = (row["no"] - 1) // 25
    for budget in (0, 1, 5, 20, 50, 100):
        a = allocate(rows, routes, max_diagrams=budget)
        assert sum(info["display"] == "image" for info in a.values()) <= budget
        chapter, has_image = None, False
        for row in rows:
            if row["chapter_index"] != chapter:
                has_image = False
            chapter = row["chapter_index"]
            display = a[row["no"]]["display"]
            if display == "hold":
                assert has_image, (budget, row["no"])
            else:
                has_image = display == "image"


def test_high_coverage_300_fills_hold_and_skip(tmp_path):
    rows, routes = _make(308)
    alloc = allocate(rows, routes, max_diagrams=300, beat_mode=True)
    for r in rows:
        a = alloc[r["no"]]
        r["display"] = a["display"]
        r["route"] = routes[r["no"]]["route"]

    before = sum(1 for r in rows if r["display"] == "image")
    assert before < 300

    p = SentencePipeline(
        "dummy",
        tmp_path,
        max_diagrams=300,
        beat_mode=True,
        verify_diagrams=False,
    )
    changed = p._force_high_coverage_images(rows, routes)

    assert changed > 0
    assert sum(1 for r in rows if r["display"] == "image") == 300
    assert any(routes[r["no"]]["route"] == "illustration" for r in rows if r["display"] == "image")
