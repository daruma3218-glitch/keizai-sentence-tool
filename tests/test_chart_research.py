"""出典のある実データのグラフ（chart_research.py・2026-09-27 社長「それで進めて」）。"""
import json

from chart_research import pick_target_no, research_charts, validate_research
from renderer import _time_positions, render_chart

SERIES = [{"label": "1984", "value": 100}, {"label": "1995", "value": 90},
          {"label": "2014", "value": 60}, {"label": "2025年7月", "value": 55}]
ITEM = {"nos": [1, 2, 5], "chart_type": "line", "title": "うすしお味の内容量", "unit": "グラム",
        "series": SERIES, "source_note": "カルビー公式・各社報道",
        "source_url": "https://www.calbee.co.jp/", "as_of": "1984〜2025年7月"}
ROWS = [
    {"no": 1, "sentence": "昔は、100グラム。"},
    {"no": 2, "sentence": "それが今は、55グラム。"},
    {"no": 5, "sentence": "1984年には100グラム入っていたものが、2025年7月、ついに55グラムになりました。"},
    {"no": 9, "sentence": "しかも、最近はさらに値段が上がってしまう。"},
]


def test_valid_research_becomes_a_line_spec_with_source():
    spec, why = validate_research(ITEM, ROWS[2]["sentence"])
    assert why == "" and spec["chart_type"] == "line" and spec["show_change"] is True
    assert spec["source_note"] == "カルビー公式・各社報道"
    assert spec["research"]["source_url"] == "https://www.calbee.co.jp/"


def test_research_without_source_is_rejected():
    spec, why = validate_research({**ITEM, "source_url": ""}, ROWS[2]["sentence"])
    assert spec is None and "出典" in why


def test_research_missing_the_script_numbers_is_rejected():
    # 原稿の 55グラム が入っていない（出典が原稿と食い違う）
    item = {**ITEM, "series": [{"label": "1984", "value": 100}, {"label": "1995", "value": 90},
                               {"label": "2025年7月", "value": 50}]}
    spec, why = validate_research(item, ROWS[2]["sentence"])
    assert spec is None and "55" in why


def test_too_few_points_are_rejected():
    spec, why = validate_research({**ITEM, "series": SERIES[:2]}, "100 55")
    assert spec is None and "時点" in why


def test_the_chart_goes_to_the_sentence_with_most_numbers():
    assert pick_target_no([1, 2, 5], {r["no"]: r for r in ROWS}) == 5


def test_research_charts_uses_web_search_and_caps_results():
    calls = []

    def fake_generate(system, query, **kw):
        calls.append(kw)
        return json.dumps([ITEM, {**ITEM, "nos": [9], "source_url": ""}], ensure_ascii=False), {}

    logs = []
    out = research_charts(ROWS, title="ステルス値上げ", max_n=5,
                          log=lambda *a, **k: logs.append(a[1]), generate=fake_generate)
    assert list(out) == [5]
    assert calls[0]["use_search"] is True
    assert any("出典" in m for m in logs)


def test_research_failure_keeps_number_cards():
    def boom(*a, **k):
        raise RuntimeError("worker_unavailable")
    assert research_charts(ROWS, generate=boom) == {}


def test_year_labels_are_placed_by_real_time(tmp_path):
    assert _time_positions(["1984", "1995年", "2025年7月"]) == [1984.0, 1995.0, 2025.5]
    assert _time_positions(["東京", "大阪"]) is None
    spec = {"chart_type": "line", "title": "内容量", "unit": "グラム", "series": SERIES,
            "source_note": "試験", "show_change": True}
    assert render_chart(spec, tmp_path / "line.png")
    assert (tmp_path / "line.png").stat().st_size > 10_000
