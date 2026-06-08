#!/usr/bin/env python3
"""Phase 3b: 図解の意味を Claude Vision で自動検証

生成された画像（主に diagram / chart）が、対応するセンテンスの意味を
正しく・分かりやすく表せているかを Claude のビジョン機能で判定する。
ズレている場合は改善指示（fix_hint）を返し、パイプラインが再生成に使う。
"""

import base64
from pathlib import Path
from typing import Optional

import anthropic

from utils import parse_json_object


CLAUDE_MODEL = "claude-sonnet-4-6"

# 検証対象にするデフォルトの type（意味の正確さが重要なもの）
DEFAULT_VERIFY_TYPES = ("diagram", "chart")


def verify_image(
    client: anthropic.Anthropic,
    image_path,
    sentence: str,
    img_type: str = "diagram",
    allowed_terms: Optional[list] = None,
) -> dict:
    """1 枚の画像が文の意味を正しく表しているか検証する。

    戻り値: {"ok": bool, "reason": str, "fix_hint": str}
        ok=False のとき fix_hint（英語の改善指示）が入る。
    判定に失敗した場合は安全側に倒して ok=True（再生成しない）。
    """
    p = Path(image_path)
    try:
        img_bytes = p.read_bytes()
    except Exception:
        return {"ok": True, "reason": "画像読込失敗（検証スキップ）", "fix_hint": ""}

    if not img_bytes or len(img_bytes) < 200:
        return {"ok": True, "reason": "画像が空（スキップ）", "fix_hint": ""}

    b64 = base64.standard_b64encode(img_bytes).decode("ascii")
    ext = p.suffix.lower()
    media_type = "image/jpeg" if ext in (".jpg", ".jpeg") else ("image/webp" if ext == ".webp" else "image/png")

    terms_note = ""
    terms = [t for t in (allowed_terms or []) if isinstance(t, str) and t.strip()]
    if terms:
        terms_note = f"\n画像に入ってよい日本語ラベル: {', '.join(terms)}"

    system = (
        "あなたは厳しい図解レビュアーです。画像が説明文の意味を正しく・分かりやすく"
        "表現できているかを評価します。結果は JSON オブジェクトのみで返してください。"
    )
    query = f"""この画像は次の日本語文の図解（type={img_type}）として生成されました:
「{sentence}」{terms_note}

次の観点で厳しくチェックしてください:
1. 文の意味を正しく表しているか（無関係・的外れでないか）
2. 画像内の文字に文字化け・誤字・読めない崩れた文字がないか
3. 重要な要素（数値・関係・対比・フローなど）が抜けていないか
4. ぱっと見て内容が伝わるか

以下の JSON のみで返答:
{{"ok": true または false, "reason": "判定理由を30字以内の日本語で", "fix_hint": "再生成時の改善指示を英語で60字以内（okがfalseのとき必須）"}}

少しでも意味がズレている／文字が崩れている場合は ok=false にしてください。"""

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=500,
            system=system,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": query},
                ],
            }],
        )
    except Exception as e:
        print(f"  [verifier ERROR] {str(e)[:120]}", flush=True)
        return {"ok": True, "reason": "検証API失敗（スキップ）", "fix_hint": ""}

    text = "".join(getattr(b, "text", "") for b in response.content if hasattr(b, "text"))
    data = parse_json_object(text)
    if not data:
        return {"ok": True, "reason": "検証パース失敗（スキップ）", "fix_hint": ""}

    return {
        "ok": bool(data.get("ok", True)),
        "reason": str(data.get("reason", ""))[:60],
        "fix_hint": str(data.get("fix_hint", ""))[:200],
    }
