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
# PC の窓口（subsk-worker）が許可している用途名。Web検索つき（kind=research）はこの名前で通る。
# 9/27 本番で独自の名前（sentence-chart-research）が tool_or_kind_not_allowed で止められた
RESEARCH_TOOL = "sentence-web-search"

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
- 比べる条件（同じ商品・同じ規格・同じ集計方法）がそろった値だけを1本の線・1組の棒にする。
  規格やサイズが途中で変わる場合は、変わったことが分かる見出しにし、as_of に条件を書く
- source_note は画面に出す短い出典名だけ（URL・リンクの書式は入れない）。URL は source_url に
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


_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)?")
_URL_RE = re.compile(r"https?://\S+")


def clean_source_note(note: str) -> str:
    """画面に出す出典名から、リンクの書式と URL を取り除く（9/27 試験で「[NewSphere…](https://…」が入った）。"""
    text = _MD_LINK_RE.sub(lambda m: m.group(1), str(note or ""))
    text = _URL_RE.sub("", text)
    text = re.sub(r"[\[(（]\s*$", "", text).lstrip("[").strip(" 　,、・/|")  # 閉じていない括弧だけ落とす
    return re.sub(r"\s{2,}", " ", text)[:40]


def _source_of(item: dict) -> tuple:
    """(出典URL, 画面の出典名)。AI が別の欄名（source / sources / url）や出典名の中に URL を
    書くことがあるので広く拾う（9/27 本番で source_url が無く、実データを使えなかった）。"""
    texts, url = [], str(item.get("source_url") or item.get("url") or "").strip()
    for key in ("source_note", "source", "source_name"):
        if isinstance(item.get(key), str):
            texts.append(item[key])
    sources = item.get("sources")
    if isinstance(sources, dict):
        sources = [sources]
    for src in sources if isinstance(sources, list) else []:
        if isinstance(src, dict):
            url = url or str(src.get("url") or src.get("source_url") or "").strip()
            texts.append(str(src.get("title") or src.get("name") or ""))
        elif isinstance(src, str):
            texts.append(src)
    if not url:
        for t in texts:
            m = _URL_RE.search(t)
            if m:
                url = m.group(0).rstrip(".,;:)」）]")
                break
    note = next((clean_source_note(t) for t in texts if clean_source_note(t)), "")
    # URL の欄にリンクの書式（[記事名](https://…)）が入ることがある（9/27 本番）→ URL 部分だけにする
    m = _URL_RE.search(url or "")
    url = m.group(0).rstrip(".,;:)」）]") if m else ""
    if url and not note:
        note = re.sub(r"^https?://(www\.)?", "", url).split("/")[0][:40]
    return url, note


def describe_item(item) -> str:
    """使わなかった候補を記録に残すための短い説明（欄名と出典の欄だけ）。"""
    if not isinstance(item, dict):
        return type(item).__name__
    keys = ",".join(sorted(item))[:80]
    src = {k: str(item.get(k))[:60] for k in ("source_url", "source_note", "source", "sources", "url") if k in item}
    return f"欄={keys} 出典={json.dumps(src, ensure_ascii=False)[:160]}"


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
    url, note = _source_of(item)
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
        "source_note": note,
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
                        tool=RESEARCH_TOOL, timeout=RESEARCH_TIMEOUT, max_tokens=6000)[0]
    except Exception as e:
        log("chart_research", f"実データの調査を省略（{str(e)[:80]}）。数字カードのまま")
        return {}
    items = parse_json_array(text) or []
    if not items:
        log("chart_research", "実データの候補なし（数字カードのまま）", (text or "")[:300])
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
            log("chart_research", f"№{target} 実データを使わない（{why}）", describe_item(item))
            continue
        out[target] = spec
        log("chart_research",
            f"№{target} 実データのグラフに（{spec['chart_type']}・{len(spec['series'])}点・出典 {spec['source_note']}）",
            json.dumps(spec["research"], ensure_ascii=False))
    log("chart_research", f"実データのグラフ: {len(out)} 件（上限 {max_n}）")
    return out
