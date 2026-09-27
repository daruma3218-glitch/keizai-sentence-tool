#!/usr/bin/env python3
"""limb_qa.py — 人物イラストの腕・手の崩れ（腕が3本など）を2つのAIで検品する。

2026-09-27 社長「№12・イラストは腕が3本になっているね。こういうミスがおきないようにしよう」
（ステルス値上げ回 20260927_113040 №12: 先生があごに手を当てたうえで両手を広げ、腕が3本）。

大人の学び直しTVの素材レポート（2026-09-24「腕の生成がミスっているイラストが残っているのはダメ」）と
同じやり方:
- 判定役は Opus 5.5 と GPT-6 Sol。PC のサブスクCLIで同時に（従量APIへは切り替えない）。
  1つだけだと見逃しがあった（素材レポートの実測で Sol が8回中1回見逃し、Opus が止めた）
- 判定役には「人物ごとの手の位置と、袖をたどった先（右肩・左肩・胸・首）」だけを答えさせ、
  合否はコードで決める: 手が3つ以上／胸や首から生えた腕／同じ肩から2本／明らかな崩れの指摘
- どちらか1つでも不合格なら不合格。どちらも判定できなければ「未確認」（作り直さない）
"""

import base64
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

JUDGES = (("claude-opus-5-5", "medium"), ("gpt-6-sol", "medium"))
JUDGE_TIMEOUT = 180
FIX_HINT = ("Every person has exactly two arms and two hands, each coming from a shoulder. "
            "Give each person one simple gesture (for example a hand on the chin, OR open palms, never both).")

SYSTEM = "あなたはイラストの検品担当です。添付画像を見て、指示された形式の JSON だけを返してください。ほかの操作はしません。"

PROMPT = """1枚目はこのチャンネルの先生キャラクターの基準画像、2枚目が検品する場面イラストです。
2枚目に描かれている人物（先生も、ほかの人も）を1人ずつ、左から順に確認してください。
1. その人物の手（握りこぶし・指さし・開いた手・物を持つ手・あごに当てた手を含む）をすべて探し、1つずつ
   位置 box_2d [ymin, xmin, ymax, xmax]（画像全体に対する0〜1000）と、その手から袖をたどった先が体のどこか
   （"右肩" "左肩" "胸・胴の途中" "首" "見えない"）を書く。
   腕を曲げて体の前に手がある・あごに手を当てている場合も、袖が肩から続いていれば "右肩" か "左肩"。
   机や人の陰で見えない手は書かない。ほかの人物の手は、その人物の欄に書く。
2. other_problems: 腕や脚が3本ある、体のつながりが不自然、指が極端に多いなど、手足と体の形の崩れだけを短く。無ければ空。
3. minor_notes: 作り直すほどではない小さな点。無ければ空。
この検品は手足と体の形だけを見る。次は崩れではないので other_problems に書かない:
表情・ポーズ・服のしわの描き分け、人物の見た目の違い（眼鏡の有無・髪型・服装・年齢）。
先生以外の人物（会社員・客・店員など）は、先生と見分けがつくよう眼鏡なし・別の服で描く決まりなので、
眼鏡のない人物は「先生の描き間違い」ではなく一般の人物として扱う（who に「ほかの人」と書く）。
JSON だけを返す: {"figures": [{"who": "先生", "hands": [{"box_2d": [0,0,0,0], "attached_to": "右肩"}]}],
 "other_problems": [], "minor_notes": []}"""


def _figure_problems(figure: dict) -> list:
    merged = []  # 同じ手の二重検出（箱の中心が近いもの）は1つにまとめる
    for hand in figure.get("hands") or []:
        if not isinstance(hand, dict):
            continue
        box = hand.get("box_2d") or [0, 0, 0, 0]
        try:
            cy, cx = (float(box[0]) + float(box[2])) / 2, (float(box[1]) + float(box[3])) / 2
        except (TypeError, ValueError, IndexError):
            cy = cx = -1000.0
        if any(abs(cy - y) < 60 and abs(cx - x) < 60 for y, x, _ in merged):
            continue
        merged.append((cy, cx, str(hand.get("attached_to") or "")))
    attached = [a for _, _, a in merged]
    problems = []
    if len(merged) >= 3:
        problems.append(f"手が{len(merged)}つある")
    if any(a in ("胸・胴の途中", "首") for a in attached):
        problems.append("胸や首から腕が生えている")
    shoulders = [a for a in attached if a in ("右肩", "左肩")]
    if len(shoulders) != len(set(shoulders)):
        problems.append("同じ肩から腕が2本出ている")
    return problems


def limb_verdict(data: dict) -> dict:
    """判定役の答えから合否を決める（判定はコード側で固定）。"""
    problems = []
    for index, figure in enumerate(f for f in (data.get("figures") or []) if isinstance(f, dict)):
        who = str(figure.get("who") or f"{index + 1}人目")[:10]
        problems += [f"{who}: {p}" for p in _figure_problems(figure)]
    problems += [str(p)[:60] for p in (data.get("other_problems") or []) if str(p).strip()][:3]
    notes = [str(n)[:60] for n in (data.get("minor_notes") or []) if str(n).strip()][:3]
    return {"ok": not problems, "problems": problems, "notes": notes}


def combine(verdicts: list) -> dict:
    """1つでも不合格なら不合格。不合格が無く合格が1つ以上なら合格。どれも判定できなければ ok=None（未確認）。"""
    failed = [v for v in verdicts if v.get("ok") is False]
    passed = [v for v in verdicts if v.get("ok") is True]
    ok = False if failed else True if passed else None
    problems = list(dict.fromkeys(p for v in failed for p in v.get("problems") or []))[:4]
    return {"ok": ok, "problems": problems, "judges": verdicts}


def _attachment(path, max_side: int = 1024) -> dict:
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=88)
    return {"media_type": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode("ascii")}


def _judge_once(model: str, effort: str, attachments: list, generate, job_id: str) -> dict:
    try:
        text, _payload = generate(SYSTEM, PROMPT, model=model, effort=effort, attachments=attachments,
                                  allow_fallback=False, tool="sentence", channel="keizai",
                                  label="先生の手足の検品", job_id=job_id, timeout=JUDGE_TIMEOUT,
                                  max_tokens=2000)
        m = re.search(r"\{[\s\S]*\}", text or "")
        verdict = limb_verdict(json.loads(m.group(0))) if m else {"ok": None, "problems": ["判定を読めない"]}
    except Exception as e:
        verdict = {"ok": None, "problems": [f"判定できず（{str(getattr(e, 'reason', '') or type(e).__name__)[:40]}）"]}
    verdict["judge"] = model
    return verdict


def check_limbs(image_path, reference_path: str = "", *, job_id: str = "", generate=None) -> dict:
    """1枚を2つの判定役で同時に検品する。戻り値は combine() の形。"""
    if generate is None:
        import subscription_runtime as _subscription
        generate = _subscription.generate
    attachments = []
    if reference_path and Path(reference_path).exists():
        attachments.append(_attachment(reference_path, max_side=640))
    attachments.append(_attachment(image_path))
    with ThreadPoolExecutor(max_workers=len(JUDGES)) as pool:
        verdicts = list(pool.map(lambda j: _judge_once(j[0], j[1], attachments, generate, job_id), JUDGES))
    return combine(verdicts)
