#!/usr/bin/env python3
"""chart_research.py — 原稿の数字を、出典のある実データのグラフにする（大人の学び直しTVの素材レポート型）。

2026-09-27 社長「実際のグラフとかを描くのはできないかな？大人の学び直しTVみたいなレポートを
描写できるといいよね」→「それで進めて」（1回5枚まで・出典つき・先生入りの絵柄に描き直し）。

いまのグラフは原稿の文にある数字だけの数字カード（例: 100グラム→55グラム）。数字が推移・比較を
語っている文について、AI に Web で出典のある実データ（年ごとの推移など）を調べさせ、
素材レポートの白背景グラフと同じ決まりで折れ線・棒グラフにする。

守ること（素材レポートの WHITE_GRAPH_POLICY と同じ考え方）:
- 出典（URL と短い出典名）が無いデータは使わない。補間・推計で埋めない。予測は使わない
- 原稿の文の数字（量・年）がグラフに入っていること（コードで照合）。入らなければ今の数字カードのまま
- 折れ線は 3〜30 時点・年は実際の間隔で並べる。棒は 2〜8 件
- 1回の生成で CHART_RESEARCH_MAX 件まで（調べる AI は PC 経由。取り合うと他の工程が時間切れになる）
"""

import json
import re
from typing import Callable, Optional

try:
    from . import subscription_runtime as _subscription
except ImportError:
    import subscription_runtime as _subscription

from utils import parse_json_array

CHART_RESEARCH_MAX = 5
RESEARCH_MODEL = "gpt-6-astra"
RESEARCH_TIMEOUT = 600

SYSTEM = (
    "あなたは教養系YouTube動画のデータリサーチャーです。原稿の数字について、Webで一次情報・信頼できる"
    "出典を調べ、グラフ用の実データを JSON で返します。見つからないものは見つからないと答え、"
    "数字を推測・補間・創作しません。"
)

QUERY_TEMPLATE = """次は動画「{title}」の原稿のうち、数字がグラフになっている文です（№・文・前後の文脈）。
{rows}

この中から、実データのグラフにすると視聴者の理解が深まるものを最大 {max_n} 件選び、Webで調べて
出典のある実データを集めてください。例: 「1984年は100グラム、2025年は55グラム」なら、
その間の年ごとの内容量の推移（公式発表・報道などで確認できる時点のみ）。

決まり:
- 数字は出典で確認できたものだけ。補間・推計・予測は入れない。確認できない時点は入れない
- 原稿の文に出てくる数字（量・金額・年）は必ずグラフに含める（原稿と食い違う出典なら、その件は返さない）
- 折れ線（line）は時点の推移に使い 3〜30 時点。label は「1984」「2025年7月」のような時点
- 棒（bar）は同じ時点の比較に使い 2〜8 件
- 2件目以降と重なる内容（同じ推移）は1件にまとめ、nos に対応する文の№をすべて書く
- 実データが見つからない・原稿の数字だけで十分なものは返さない

以下の JSON 配列のみで返答（該当なしなら []）:
[{{"nos": [5], "chart_type": "line", "title": "15字以内の見出し", "unit": "グラム",
   "series": [{{"label": "1984", "value": 100}}, {{"label": "2025年7月", "value": 55}}],
   "source_note": "画面に出す短い出典名（例: カルビー公式・各社報道）",
   "source_url": "https://...（主な出典のURL）", "as_of": "対象の時点・期間"}}]"""

_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
_YEAR_RE = re.compile(r"^\s*(\d{4})(?:年)?(?:\s*(\d{1,2})月)?\s*$")


def _numbers(text: str) -> list:
    out = []
    for m in _NUM_RE.findall((text or "").translate(str.maketrans("０１２３４５６７８９．，", "0123456789.,"))):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            continue
    return out


def validate_research(item: dict, sentences: str) -> tuple:
    """(使える chart_spec, 使えない理由)。コードで出典・形・原稿の数字との一致を確かめる。"""
    if not isinstance(item, dict):
        return None, "形式が不正"
    ctype = item.get("chart_type")
    series = item.get("series") or []
    if ctype not in ("line", "bar") or not isinstance(series, list):
        return None, "折れ線・棒ではない"
    points = []
    for it in series:
        if not isinstance(it, dict):
            continue
        try:
            points.append((str(it.get("label", "")).strip(), float(it.get("value"))))
        except (TypeError, ValueError):
            return None, "数値でない値がある"
    if ctype == "line" and not 3 <= len(points) <= 30:
        return None, f"折れ線の時点数が {len(points)}（3〜30）"
    if ctype == "bar" and not 2 <= len(points) <= 8:
        return None, f"棒の件数が {len(points)}（2〜8）"
    url = str(item.get("source_url") or "").strip()
    note = str(item.get("source_note") or "").strip()
    if not re.match(r"https?://[^\s]+\.[^\s]+", url) or not note:
        return None, "出典（URL・出典名）が無い"
    values = {round(v, 6) for _, v in points}
    labels = " ".join(label for label, _ in points)
    label_nums = {round(n, 6) for n in _numbers(labels)}
    missing = [n for n in _numbers(sentences)
               if round(n, 6) not in values and round(n, 6) not in label_nums]
    if missing:
        return None, "原稿の数字がグラフに無い: " + ", ".join(f"{m:g}" for m in missing[:4])
    spec = {
        "chart_type": ctype,
        "title": str(item.get("title") or "").strip()[:30],
        "unit": str(item.get("unit") or "").strip()[:10],
        "series": [{"label": label, "value": v} for label, v in points],
        "source_note": note[:40],
        "show_change": ctype == "line",
        "research": {"source_url": url, "as_of": str(item.get("as_of") or "")[:60]},
    }
    return spec, ""


def pick_target_no(nos: list, rows_by_no: dict) -> Optional[int]:
    """調べたグラフを載せる文。原稿の数字を最も多く含む文（同数なら先の文）。"""
    candidates = [n for n in nos if n in rows_by_no]
    if not candidates:
        return None
    return max(candidates, key=lambda n: (len(_numbers(rows_by_no[n].get("sentence", ""))), -n))


def research_charts(chart_rows: list, *, title: str = "", max_n: int = CHART_RESEARCH_MAX,
                    log: Optional[Callable] = None, generate=None) -> dict:
    """数字カードになる文から最大 max_n 件を選んで実データを調べる。戻り値 {no: chart_spec}。

    generate は subscription_runtime.generate 互換（テストで差し替える）。失敗しても {} を返す。
    """
    log = log or (lambda *a, **k: None)
    generate = generate or _subscription.generate
    rows_by_no = {r["no"]: r for r in chart_rows if r.get("no") is not None}
    if not rows_by_no:
        return {}
    lines = []
    for r in chart_rows[:40]:
        ctx = (r.get("block_text") or "").replace("\n", " ")[:160]
        lines.append(f"№{r['no']}: {r.get('sentence', '')}\n  （前後: {ctx}）")
    query = QUERY_TEMPLATE.format(title=title or "（無題）", rows="\n".join(lines), max_n=max_n)
    try:
        text = generate(SYSTEM, query, model=RESEARCH_MODEL, use_search=True,
                        tool="sentence-chart-research", timeout=RESEARCH_TIMEOUT, max_tokens=6000)[0]
    except Exception as e:
        log("chart_research", f"実データの調査を省略（{str(e)[:80]}）。数字カードのまま")
        return {}
    items = parse_json_array(text) or []
    out = {}
    for item in items[: max_n * 2]:
        if len(out) >= max_n:
            break
        nos = [n for n in (item.get("nos") or []) if isinstance(n, int)] if isinstance(item, dict) else []
        target = pick_target_no(nos, rows_by_no)
        if target is None or target in out:
            continue
        sentences = " ".join(rows_by_no[n].get("sentence", "") for n in nos if n in rows_by_no)
        spec, why = validate_research(item, sentences)
        if not spec:
            log("chart_research", f"№{target} 実データを使わない（{why}）")
            continue
        out[target] = spec
        log("chart_research",
            f"№{target} 実データのグラフに（{spec['chart_type']}・{len(spec['series'])}点・出典 {spec['source_note']}）",
            json.dumps(spec["research"], ensure_ascii=False))
    log("chart_research", f"実データのグラフ: {len(out)} 件（上限 {max_n}）")
    return out
