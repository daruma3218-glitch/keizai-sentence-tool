#!/usr/bin/env python3
"""Phase 3b: 図解の意味を ASTRA CLI で自動検証

生成された画像（主に diagram / chart）が、対応するセンテンスの意味を
正しく・分かりやすく表せているかを ASTRA の画像入力で判定する。
ズレている場合は改善指示（fix_hint）を返し、パイプラインが再生成に使う。
"""

try:
    from . import subscription_runtime as _subscription
except ImportError:
    import subscription_runtime as _subscription


import base64
import json
import os
from pathlib import Path
from typing import Optional

import anthropic

from utils import parse_json_object


# v3 Step5: chart/map が renderer 化されたため、Vision 検品は diagram のみに縮小。
# 原稿と完成図の意味・ラベルをASTRAのサブスクCLIで検品する。
CLAUDE_MODEL = os.environ.get("VERIFY_MODEL", "").strip() or "gpt-6-astra"

# 検証対象にするデフォルトの type（chart は決定論レンダリングのため検品不要）
DEFAULT_VERIFY_TYPES = ("diagram",)

# 世界観ロック中の画風チェックの観点（2026-09-25 カラクリ経済学）。
# 「画風を合わせて」という抽象的な言い方では判定役が見逃すため、見る場所を具体的に書く
# （すあし社長の顔の判定と同じ教訓）。合否は style_score からコードが決める（style_ok）。
# 既存画像（ルノアール回）での検証で調整した:
#  1回目（合否）線の太さの差やシャツの襟まで不合格にし、残すべき画像の8/9を落とした
#  2回目（合否・迷ったら合格）つやのあるビール・質感の建物・年上に見える先生を見逃した
#  3回目（合否・描き方を列挙）先生の上着のツイード柄や小さな光まで不合格にした（6/13）
# 合否の二択は言い回しで大きく振れるため、5段階の点数で答えさせ、合格線はコードが決める。
#  4回目（点数）実写1・若者/別人の先生2・つや/質感3・残す画像4。図解は基準画像（人物）と比べるのが
#  合わないため対象外（アイコン調の図解を2点にした）。
#  5回目（最終設定: 基準画像640px・イラスト17枚）3点以下を作り直すと off 6/7 を拾い、on の誤りは 2/10
#  （どちらも3点）。2点以下なら誤り0だが、新居先生が最も多く差し替えた「厚塗り」（つや・質感）を見逃す。
#  誤って作り直しても、点数が上がらなければ元の画像に戻すので悪化しない（費用は1枚分）。
STYLE_SCORE_REGENERATE_MAX = 3   # この点数以下は作り直す

STYLE_CHECK_RULES_JA = """【画風チェック（このチャンネルは世界観ロック）】
1枚目はチャンネルの基準画像（先生キャラと画風の見本）、2枚目が判定する画像です。
2枚目を1枚目の隣に並べたとき、同じシリーズの絵に見えるかを style_score（1〜5）で答えてください。
- 5: 同じシリーズ。べた塗りのフラットな漫画イラストで、線・塗り・人物の描き方がそろっている
- 4: ほぼ同じシリーズ。線の太さ、小さな光やきらめき、布や小物の柄、背景色などの細部だけ違う
- 3: 並べると少し浮く。つややグラデーションの立体感、描き込みの多さが目立つが、フラットなイラストではある
- 2: 別シリーズに見える。厚塗り・劇画調・アニメ調・水彩・鉛筆画、先生が別人（白髪まじり・年上・
     顔のしわ・髪型や眼鏡や服の色が違う）、若い学生やパーカー姿の若者が主役として大きく描かれている
- 1: 実写・写真風・3DCG
客・店員・店主・会社員など一般の大人が同じ画風で描かれているのは問題なし。
先生の上着のツイード柄は基準画像にもある特徴なので減点しない。
画風の違いは style_score だけで表し、ok（意味・文字）の判定理由にはしないでください。"""


def verify_image(
    client: anthropic.Anthropic,
    image_path,
    sentence: str,
    img_type: str = "diagram",
    allowed_terms: Optional[list] = None,
    block_context: str = "",
    chapter: str = "",
    theme: str = "",
    diagram_blueprint: Optional[dict] = None,
    style_reference_path=None,
    style_rules: str = "",
) -> dict:
    """1 枚の画像が、原稿の文脈の中で文の意味を正しく表しているか検証する。

    単独の文だけでなく、動画テーマ・章・前後段落（block_context）も渡して
    「文脈の中でこの文が本当に意味すること」と図が合っているかを判定する。

    style_reference_path（チャンネルの基準画像）と style_rules（世界観の設定文）を渡すと、
    同じ呼び出しで画風チェックもする（基準画像を1枚目・判定画像を2枚目として添付）。

    戻り値: {"ok": bool, "reason": str, "fix_hint": str}
        ok=False のとき fix_hint（英語の改善指示）が入る。
        画風チェック時は "style_score"（1〜5）・"style_ok"（点数から判定、読めなければ None）・"style_issue" も入る。
    判定できない場合は未確認として返し、合格扱いにしない。
    """
    p = Path(image_path)
    try:
        img_bytes = p.read_bytes()
    except Exception:
        return {"ok": False, "verified": False, "reason": "画像読込失敗（未確認）", "fix_hint": ""}

    if not img_bytes or len(img_bytes) < 200:
        return {"ok": False, "verified": False, "reason": "画像が空（未確認）", "fix_hint": ""}

    b64, media_type = _encode_for_review(img_bytes, p.suffix)
    img_bytes = None  # 元のバイト列を早期解放（メモリ削減）

    style_ref = None
    if style_reference_path and (style_rules or "").strip():
        try:
            ref_bytes = Path(style_reference_path).read_bytes()
            if len(ref_bytes) > 200:
                # 基準画像は毎回同じ。本番ではSupabase経由でPCへ送るため、顔と画風が分かる
                # 640pxに縮めて転送量を抑える（1024pxの約半分）
                style_ref = _encode_for_review(ref_bytes, Path(style_reference_path).suffix, max_side=640)
        except Exception:
            style_ref = None  # 基準画像が読めなければ画風チェックは省く（意味の検品は続ける）

    terms_note = ""
    terms = [t for t in (allowed_terms or []) if isinstance(t, str) and t.strip()]
    if terms:
        terms_note = f"\n画像に入ってよい日本語ラベル: {', '.join(terms)}"

    # 文脈ブロック（テーマ・章・前後段落）を組み立てる
    ctx_parts = []
    if theme.strip():
        ctx_parts.append(f"【動画全体のテーマ】{theme.strip()[:120]}")
    if chapter.strip():
        ctx_parts.append(f"【この図が属する章】{chapter.strip()}")
    if block_context.strip():
        ctx_parts.append(f"【前後の文脈（この文を含む段落）】\n{block_context.strip()[:500]}")
    context_block = "\n".join(ctx_parts)
    if context_block:
        context_block = "===== 原稿の文脈 =====\n" + context_block + "\n=====================\n\n"

    blueprint_block = ""
    if isinstance(diagram_blueprint, dict) and diagram_blueprint:
        try:
            blueprint_block = (
                "\n===== 図解設計JSON =====\n"
                + json.dumps(diagram_blueprint, ensure_ascii=False)[:1000]
                + "\n========================\n"
            )
        except Exception:
            blueprint_block = ""

    system = (
        "あなたは厳しい図解レビュアーです。動画原稿の文脈を踏まえて、画像がその文の"
        "意味を正しく・分かりやすく表現できているかを評価します。"
        "結果は JSON オブジェクトのみで返してください。"
    )
    image_note = "この画像は"
    style_block = ""
    style_fields = ""
    if style_ref:
        image_note = "2枚目の画像は"
        style_block = (
            f"\n\n{STYLE_CHECK_RULES_JA}\n世界観の設定文:\n---\n{style_rules.strip()[:3000]}\n---"
        )
        style_fields = (
            ', "style_score": 1〜5 の整数（上の画風チェック）, '
            '"style_issue": "4以下のとき、何が違うかを30字以内の日本語で"'
        )
    query = f"""{context_block}{blueprint_block}{image_note}、上の文脈の中の次の1文の図解（type={img_type}）として生成されました:
「{sentence}」{terms_note}

**文脈を踏まえて**、次の観点で厳しくチェックしてください:
1. 文脈の中でこの文が「本当に伝えたい内容」を正しく表しているか
   （例: 主語・対象・何が変化したか等が文脈で決まる場合、それを正しく描けているか。
    文単独では曖昧でも、文脈から読み取れる正しい内容と図がズレていないか）
2. 図の内容が原稿の主張と矛盾・的外れになっていないか
3. 画像内の文字に文字化け・誤字・読めない崩れた文字がないか
4. 重要な要素（数値・関係・対比・フロー・主体）が抜けていないか
5. ぱっと見て内容が伝わるか
6. 図解設計JSONがある場合、visual_goal / structure / reading_path / relationships に沿っているか
7. ラベルと矢印を順に追うだけで意味が入ってくるか。孤立したキーワード羅列になっていないか
8. 文字が重なっていないか、ラベルが多すぎないか、線や棒が文字の前に出ていないか{style_block}

以下の JSON のみで返答:
{{"ok": true または false, "reason": "判定理由を40字以内の日本語で", "issue_tags": ["meaning_mismatch | unreadable_text | weak_structure | keyword_list | overcrowded | label_overlap | missing_key_element | factual_risk"], "fix_hint": "再生成時の改善指示を英語で80字以内（okがfalseのとき必須・文脈の正しい内容を反映）"{style_fields}}}

文脈と図がズレている／文字が崩れている場合は ok=false にしてください。"""

    content = []
    if style_ref:
        content.append({"type": "image", "source": {"type": "base64", "media_type": style_ref[1], "data": style_ref[0]}})
    content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}})
    content.append({"type": "text", "text": query})

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=600 if style_ref else 500,
            system=system,
            timeout=180, effort="high", workload="assets_review",
            messages=[{
                "role": "user",
                "content": content,
            }],
        )
    except _subscription.SubscriptionUnavailable:
        raise
    except Exception as e:
        print(f"  [verifier ERROR] {str(e)[:120]}", flush=True)
        return {"ok": False, "verified": False, "reason": "検証を完了できませんでした（未確認）", "fix_hint": ""}

    text = "".join(getattr(b, "text", "") for b in response.content if hasattr(b, "text"))
    data = parse_json_object(text)
    if not isinstance(data, dict) or type(data.get("ok")) is not bool or not isinstance(data.get("reason"), str):
        return {"ok": False, "verified": False, "reason": "検証結果を読み取れませんでした（未確認）", "fix_hint": ""}

    result = {
        "ok": data.get("ok") is True,
        "verified": True,
        "reason": str(data.get("reason", ""))[:60],
        "issue_tags": [str(x)[:40] for x in data.get("issue_tags", []) if isinstance(x, str)][:5],
        "fix_hint": str(data.get("fix_hint", ""))[:200],
    }
    if style_ref:
        # 合否はコードが点数から決める。読み取れない点数は None＝画風は未確認（作り直さない）。
        score = data.get("style_score")
        score = score if type(score) is int and 1 <= score <= 5 else None
        result["style_score"] = score
        result["style_ok"] = None if score is None else score > STYLE_SCORE_REGENERATE_MAX
        result["style_issue"] = str(data.get("style_issue", "") or "")[:60]
    return result


def _encode_for_review(img_bytes: bytes, suffix: str = ".png", max_side: int = 1024) -> tuple:
    """検証用に縮小（長辺 max_side px・JPEG）して (base64, media_type) を返す。

    メモリ・アップロード時間・Vision のトークン/コストを大幅に削減する。
    図解の良し悪し・文字化け・画風の判定には 1024px で十分。失敗時は元画像にフォールバック。
    """
    try:
        import io
        from PIL import Image
        with Image.open(io.BytesIO(img_bytes)) as im:
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side))  # 長辺を max_side へ（アスペクト維持）
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=82)
            small = buf.getvalue()
        return base64.standard_b64encode(small).decode("ascii"), "image/jpeg"
    except Exception:
        ext = (suffix or "").lower()
        media_type = "image/jpeg" if ext in (".jpg", ".jpeg") else ("image/webp" if ext == ".webp" else "image/png")
        return base64.standard_b64encode(img_bytes).decode("ascii"), media_type
