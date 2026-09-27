#!/usr/bin/env python3
"""chart_restyle.py — コードで正確に描いたグラフを、番組の絵柄に描き直す（AI清書）。

2026-09-27 社長・せいまさん（カラクリ経済学「ステルス値上げ」回）:
コードで描くグラフ（renderer.py）は数字が正確な代わりに「単調」。ChatGPT の画像生成で
番組の絵柄に描き直したい。

やり方:
1. renderer.py が描いたグラフ（数字の正解）を1枚目、先生の基準画像を2枚目にして
   gpt-image の images.edit で、同じ数字・同じ文字のまま世界観の絵柄へ描き直す。
2. 描き直した絵と元のグラフを並べて、書かれている数字と文字を読み取り、コードで突き合わせる。
   数字が1つでも増減・食い違い、または文字（見出し・ラベル）が言い換え・追加されていれば、
   元のコードのグラフを使う（数字も言葉も正確さは下げない）。
   9/27 の試験で、数字だけ見ていると「受取額→収入」「（仮定）」の追加を見逃したため文字も比べる。
3. 元のグラフはジョブの chart_render/{no}.png に残す（images/ に置くと ZIP に混ざるため別フォルダ）。

チャンネル設定 chart_ai_restyle: true のときだけ動く（既定は従来どおりコードの図のみ）。
"""

import io
import re
import shutil
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Optional

from utils import parse_json_object

CHART_RESTYLE_INSTRUCTION = (
    "CHART REDRAW: The FIRST attached image is an exact data chart for this video. Redraw it as "
    "one frame of this video series, in the channel art style described below. "
    "Keep EVERY number, unit, label and title EXACTLY as written in the first image: the same "
    "characters, the same values, the same units, nothing added, nothing removed, nothing rounded. "
    "Copy every Japanese word character for character: do not paraphrase, shorten or reword any "
    "title or label, and do not add any new words, notes or captions (for example no added "
    "parenthetical notes). "
    "Keep the chart type, the order of the items and the relative sizes of the bars, lines or "
    "values. Do not write any other number anywhere in the image (no price tags, dates or "
    "counters that are not in the first image). "
    "Make it more engaging than the plain original: use the channel palette, a clear visual "
    "hierarchy, and one or two simple flat icons or objects that fit the topic. "
    "If a SECOND image is attached, it shows the channel's professor. You may add him once, "
    "small, beside the chart (pointing at it or reacting), drawn as that SAME person "
    "(identical face, hair, glasses, half-lidded eyes, closed-mouth smile and outfit). "
    "The chart stays the main subject."
)

VERIFY_SYSTEM = (
    "あなたは画像の文字起こし係です。画像に書かれている数字と文字を、見えたとおりに漏れなく書き出します。"
    "推測や計算はしません。結果は JSON オブジェクトのみで返してください。"
)

VERIFY_QUERY = """1枚目と2枚目の画像それぞれについて、画像の中に書かれている数字（年・金額・量・割合・順位など、
アラビア数字と漢数字の数値すべて）を、見えたとおりに書き出してください。
単位や記号（円・グラム・%・年など）は付けても付けなくてもかまいません。同じ数字が2回書かれていれば2回書きます。
アイコンの中の数字や、小さな注記の数字も含めます。

また、それぞれの画像に書かれている文字（見出し・ラベル・注記・吹き出しなど。数字も含めてよい）を、
1行ずつ見えたとおりに書き出してください。

以下の JSON のみで返答:
{"first": ["1枚目の数字", ...], "second": ["2枚目の数字", ...],
 "first_texts": ["1枚目の文字の行", ...], "second_texts": ["2枚目の文字の行", ...]}"""

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９．，", "0123456789.,")
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


def normalize_numbers(items) -> list:
    """書き出された数字を比べられる形にする（全角→半角・桁区切りを外す・末尾の .0 を外す）。"""
    out = []
    for raw in items or []:
        s = str(raw).translate(_FULLWIDTH_DIGITS)
        for m in _NUM_RE.findall(s):
            try:
                n = format(Decimal(m.replace(",", "")).normalize(), "f")
            except InvalidOperation:
                continue
            out.append(n)
    return sorted(out)


# 日本語の文字（ひらがな・カタカナ・漢字・長音）だけを比べる。記号・英字（vs・¥ など）と
# 数字は比べない（数字は numbers_match で見る）。行の区切り方の違いに左右されないよう、
# 文字の出現回数で比べる。
_JA_CHAR_RE = re.compile(r"[ぁ-んァ-ヶー一-龯々]")


def texts_match(first, second) -> tuple:
    """(一致したか, 食い違いの説明)。見出し・ラベルの日本語が言い換え・追加・欠落していないか。"""
    a = Counter(_JA_CHAR_RE.findall("".join(str(x) for x in first or [])))
    b = Counter(_JA_CHAR_RE.findall("".join(str(x) for x in second or [])))
    if a == b:
        return True, ""
    missing = "".join(sorted((a - b).elements()))
    extra = "".join(sorted((b - a).elements()))
    parts = []
    if missing:
        parts.append("消えた文字: " + missing[:20])
    if extra:
        parts.append("増えた文字: " + extra[:20])
    return False, " / ".join(parts)


def numbers_match(first, second) -> tuple:
    """(一致したか, 食い違いの説明)。数字の種類と個数が同じなら一致。"""
    a, b = normalize_numbers(first), normalize_numbers(second)
    if not a:
        return False, "元のグラフから数字を読み取れませんでした"
    if set(a) == set(b):
        return True, ""
    missing = sorted(set(a) - set(b))
    extra = sorted(set(b) - set(a))
    parts = []
    if missing:
        parts.append("消えた数字: " + ", ".join(missing[:5]))
    if extra:
        parts.append("増えた数字: " + ", ".join(extra[:5]))
    return False, " / ".join(parts)


def _image_file(data: bytes, name: str):
    bio = io.BytesIO(data)
    bio.name = name  # SDK が拡張子から MIME を判定
    return bio


def redraw_chart(openai_client, rendered_path: Path, out_path: Path, *, model: str, quality: str,
                 style_lock_text: str = "", reference_path: str = "", sentence: str = "") -> tuple:
    """描いたグラフを番組の絵柄に描き直して out_path に保存。(成功, エラー)。"""
    from generator import _save_as_16_9
    import base64

    images = [_image_file(Path(rendered_path).read_bytes(), "chart.png")]
    if reference_path and Path(reference_path).exists():
        images.append(_image_file(Path(reference_path).read_bytes(), "professor.png"))
    prompt = CHART_RESTYLE_INSTRUCTION
    if (sentence or "").strip():
        # 9/27 試験: 文を渡さないと、ポテトチップスの話にご飯茶碗を描いた
        prompt += ("\n\nTOPIC: this chart illustrates this narration sentence (Japanese): "
                   f"「{sentence.strip()[:200]}」. Choose icons and objects that match this topic. "
                   "Do not write this sentence in the image.")
    if (style_lock_text or "").strip():
        prompt += "\n\n" + style_lock_text.strip()
    try:
        response = openai_client.images.edit(
            model=model, image=images, prompt=prompt, n=1, size="1536x1024", quality=quality,
        )
        datum = response.data[0] if response and response.data else None
        b64 = getattr(datum, "b64_json", None) if datum is not None else None
        if not b64:
            return False, "画像が返りませんでした"
        _save_as_16_9(base64.b64decode(b64), out_path)
        return True, ""
    except Exception as e:
        return False, str(e)[:160]


def verify_redraw(verify_client, rendered_path: Path, redrawn_path: Path) -> tuple:
    """元のグラフと描き直した絵の数字を読み取り、コードで突き合わせる。(一致, 説明)。"""
    from verifier import _encode_for_review, CLAUDE_MODEL

    content = []
    for p in (rendered_path, redrawn_path):
        b64, media = _encode_for_review(Path(p).read_bytes(), Path(p).suffix, max_side=1280)
        content.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}})
    content.append({"type": "text", "text": VERIFY_QUERY})
    try:
        response = verify_client.messages.create(
            model=CLAUDE_MODEL, max_tokens=500, system=VERIFY_SYSTEM,
            timeout=180, effort="high", workload="assets_review",
            messages=[{"role": "user", "content": content}],
        )
    except Exception as e:
        return False, f"数字の確認ができませんでした（{str(e)[:60]}）"
    text = "".join(getattr(b, "text", "") for b in response.content if hasattr(b, "text"))
    data = parse_json_object(text)
    if not isinstance(data, dict) or not isinstance(data.get("first"), list) \
            or not isinstance(data.get("second"), list):
        return False, "数字の確認結果を読み取れませんでした"
    same, why = numbers_match(data["first"], data["second"])
    if not same:
        return False, why
    if not isinstance(data.get("first_texts"), list) or not isinstance(data.get("second_texts"), list):
        return False, "文字の確認結果を読み取れませんでした"
    return texts_match(data["first_texts"], data["second_texts"])


def restyle_chart_file(images_dir: Path, no: int, *, openai_client, verify_client, model: str,
                       quality: str, style_lock_text: str = "", reference_path: str = "",
                       sentence: str = "", log: Optional[Callable] = None) -> dict:
    """{no}.png（コードのグラフ）を描き直し、数字が一致したときだけ差し替える。

    戻り値: {"restyled": bool, "reason": str}。一致しなければ {no}.png は元のまま。
    元のグラフはジョブの chart_render/{no}.png に残す（スタッフの ZIP に混ざらないよう images/ の外）。
    """
    log = log or (lambda *a, **k: None)
    final = Path(images_dir) / f"{no}.png"
    keep_dir = Path(images_dir).parent / "chart_render"
    keep_dir.mkdir(parents=True, exist_ok=True)
    rendered = keep_dir / f"{no}.png"
    candidate = keep_dir / f"{no}_ai.png"
    if not final.exists():
        return {"restyled": False, "reason": "元のグラフがありません"}
    shutil.copyfile(final, rendered)
    ok, err = redraw_chart(openai_client, rendered, candidate, model=model, quality=quality,
                           style_lock_text=style_lock_text, reference_path=reference_path,
                           sentence=sentence)
    if not ok:
        log("chart_restyle", f"№{no} 描き直しに失敗（コードのグラフのまま）: {err}")
        return {"restyled": False, "reason": err}
    same, why = verify_redraw(verify_client, rendered, candidate)
    if not same:
        candidate.unlink(missing_ok=True)
        log("chart_restyle", f"№{no} 数字か文字が一致しないためコードのグラフのまま: {why}")
        return {"restyled": False, "reason": why}
    candidate.replace(final)
    log("chart_restyle", f"№{no} 番組の絵柄に描き直し（数字と文字の一致を確認）")
    return {"restyled": True, "reason": ""}
