#!/usr/bin/env python3
"""共通ユーティリティ - JSON パースと Claude API 呼び出し"""

try:
    from . import subscription_runtime as _subscription
except ImportError:
    import subscription_runtime as _subscription


import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

import anthropic

# rows_progress.json への書き込みを直列化する共有ロック。
# パイプラインスレッド（_dump_snapshot）と Flask リクエストスレッド（再生成の
# _update_regen_snapshot）が同じファイルを read-modify-write するため、
# 同時書き込みで片方の更新が消える競合をここで防ぐ。
SNAPSHOT_IO_LOCK = threading.Lock()


def load_env(project_root: Path) -> None:
    """.env ファイルから環境変数を読み込む

    既存の環境変数が空文字列のときも .env で上書きする
    （setdefault だと空文字を「設定済み」と判定してしまうため）。
    """
    env_path = project_root / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" not in line or line.startswith("#"):
            continue
        key, val = line.split("=", 1)
        val = val.strip().strip('"').strip("'")
        if val and val != "your_api_key_here":
            existing = os.environ.get(key, "")
            if not existing:  # 未設定 or 空文字列なら上書き
                os.environ[key] = val


def get_anthropic_client(api_key: str = "") -> anthropic.Anthropic:
    return _subscription.SubscriptionClient(tool='sentence')


# ===== Prompt cache（API代削減。中山さんの monorepo 改修 97346ca を移植）=====
PROMPT_CACHE_DISABLED_VALUES = {"0", "false", "no", "off"}
PROMPT_CACHE_MIN_SYSTEM_CHARS = 1000


def prompt_cache_enabled() -> bool:
    """PROMPT_CACHE_ENABLED=0/false/no/off のときだけ無効化する。"""
    flag = os.environ.get("PROMPT_CACHE_ENABLED", "1").strip().lower()
    return flag not in PROMPT_CACHE_DISABLED_VALUES


def cached_text_block(text: str, min_chars: int = PROMPT_CACHE_MIN_SYSTEM_CHARS) -> dict:
    """長い固定テキストブロックだけ prompt cache 対象にする。"""
    block = {"type": "text", "text": text}
    if prompt_cache_enabled() and isinstance(text, str) and len(text) >= min_chars:
        block["cache_control"] = {"type": "ephemeral"}
    return block


def cached_system_param(system: str, min_chars: int = PROMPT_CACHE_MIN_SYSTEM_CHARS):
    """長い固定 system prompt のみ prompt cache 対象にする。"""
    if prompt_cache_enabled() and isinstance(system, str) and len(system) >= min_chars:
        return [cached_text_block(system, min_chars)]
    return system


def cached_user_content(*parts) -> list:
    """(text, cacheable) の並びから Anthropic content blocks を作る。"""
    content = []
    for part in parts:
        text, cacheable = part if isinstance(part, tuple) else (part, False)
        if not text:
            continue
        content.append(cached_text_block(text) if cacheable else {"type": "text", "text": text})
    return content


def log_prompt_cache_usage(response, label: str = "Claude") -> None:
    """キャッシュが実際に読まれた/作られたときだけ軽く表示する。"""
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
    if cache_read or cache_create:
        print(f"  [CACHE] {label}: read={cache_read} create={cache_create}", flush=True)


def _flatten_query_for_gateway(query) -> str:
    """str または content blocks リストをプレーンテキスト化 (ゲートウェイ転送用)"""
    if isinstance(query, str):
        return query
    if isinstance(query, list):
        parts = []
        for b in query:
            t = b.get("text") if isinstance(b, dict) else getattr(b, "text", None)
            if t:
                parts.append(t)
        return "\n".join(parts)
    return str(query)


# チャンネル別使用量台帳のコンテキスト (2026-08-11 社長指示「チャンネルごとの管理」)。
# app.py がジョブ開始時に set_subsk_channel(channel_id) で設定し、ゲートウェイ投函
# payload に乗ってワーカー側の台帳 (usage_ledger.jsonl) に記録される。
# 注: モジュールグローバルのため、異なるチャンネルのジョブが同時並走した場合は
# 後勝ちで混ざりうる (月次のチャンネル別把握という目的には十分な精度)。
_SUBSK_CHANNEL = ""


def set_subsk_channel(channel_id: str) -> None:
    """現在処理中ジョブのチャンネル id (channels.json の id) を設定する。"""
    global _SUBSK_CHANNEL
    _SUBSK_CHANNEL = (channel_id or "").strip().lower()


def get_subsk_channel() -> str:
    return _SUBSK_CHANNEL


def claude_query(
    client: anthropic.Anthropic,
    query: "str | list",  # list = cached_user_content() が作る content blocks
    system: str,
    max_tokens: int = 4096,
    model: str = "claude-sonnet-5",
    max_retries: int = 3,
    timeout_seconds: Optional[float] = None,
    effort: Optional[str] = None,
    workload: str = "",
) -> str:
    return _subscription.generate(system, query, tool='sentence', channel=get_subsk_channel(),
        model=model, max_tokens=max_tokens, timeout=timeout_seconds or 900,
        effort=effort, workload=workload)[0]


def parse_json_array(text: str) -> list:
    """テキストからJSON配列を抽出（コードブロック対応）"""
    if not text:
        return []
    text = text.strip()
    # ```json ... ``` を剥がす
    if "```json" in text:
        text = text.split("```json", 1)[1]
        if "```" in text:
            text = text.split("```", 1)[0]
        text = text.strip()
    elif "```" in text:
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1].strip()

    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []

    candidate = text[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # 末尾欠損の修復試行
    for suffix in ['"}]', '}]', ']']:
        try:
            return json.loads(candidate + suffix)
        except json.JSONDecodeError:
            continue

    # 修復: シングルクォートをダブルクォートに置換（最終手段）
    try:
        return json.loads(candidate.replace("'", '"'))
    except json.JSONDecodeError:
        pass

    return []


def parse_json_object(text: str) -> dict:
    """テキストからJSONオブジェクトを抽出"""
    if not text:
        return {}
    text = text.strip()
    if "```json" in text:
        text = text.split("```json", 1)[1]
        if "```" in text:
            text = text.split("```", 1)[0]
        text = text.strip()
    elif "```" in text:
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1].strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}

    candidate = text[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    for suffix in ['"}]}', '"}}', '}']:
        try:
            return json.loads(candidate + suffix)
        except json.JSONDecodeError:
            continue

    return {}


def save_json(path: Path, data) -> None:
    """JSON ファイルを保存（ディレクトリも作成）"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_json(path: Path, default=None):
    """JSON ファイルを読み込み"""
    if not path.exists():
        return default if default is not None else {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default if default is not None else {}


# v3 Step7: 原稿パイプライン(otona-manabi-tv phase_e)の final.json 直結。
# final.json は { "final": "<完成原稿テキスト全体>", "tentative_title": ..,
#   "purpose": .., "fact_report": .., "structure_summary": .. , ... } 形式。
# 「final が十分な長さの文字列」であることだけを条件に final.json と判定する。
# 存在しないキーはすべて任意（原稿側の改修進度に依存しない）。
_FINAL_MIN_LEN = 50


def parse_final_json(text: str):
    """テキストが原稿パイプラインの final.json なら dict を返す。違えば None。

    - 先頭が "{" でなければ即 None（生原稿テキストはここを通って素通り）
    - JSON として壊れている / オブジェクトでない → None
    - "final" が十分な長さの文字列でなければ None（別形式の JSON を誤認しない）
    """
    if not text:
        return None
    s = text.strip()
    if not s.startswith("{"):
        return None
    try:
        obj = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    final = obj.get("final")
    if not isinstance(final, str) or len(final.strip()) < _FINAL_MIN_LEN:
        return None
    return obj


def extract_from_final_json(obj: dict) -> dict:
    """final.json の dict から、本パイプラインが使う情報を取り出す（防御的）。

    戻り値: {manuscript, title, purpose, fact_context}
      - manuscript: 本文（final）
      - title: tentative_title（無ければ ""）
      - purpose: 動画の狙い（無ければ ""）
      - fact_context: 検証済み数値・出典の文脈（fact_report + reference_list を結合）
    """
    manuscript = str(obj.get("final", "") or "")
    title = str(obj.get("tentative_title", "") or "").strip()
    purpose = str(obj.get("purpose", "") or "").strip()

    parts = []
    fr = obj.get("fact_report")
    if isinstance(fr, str) and fr.strip():
        parts.append(fr.strip())
    elif isinstance(fr, (list, dict)):
        try:
            parts.append(json.dumps(fr, ensure_ascii=False))
        except Exception:
            pass
    rl = obj.get("reference_list")
    if isinstance(rl, str) and rl.strip():
        parts.append(rl.strip())
    elif isinstance(rl, (list, dict)):
        try:
            parts.append(json.dumps(rl, ensure_ascii=False))
        except Exception:
            pass
    fact_context = "\n\n".join(parts)

    return {
        "manuscript": manuscript,
        "title": title,
        "purpose": purpose,
        "fact_context": fact_context,
    }
