#!/usr/bin/env python3
"""サブスクLLMゲートウェイ (Render側・2026-08-10 センテンスつくーる版)

目的:
    Render 上で走るセンテンスつくーるの Claude 呼び出し (opus/sonnet) を、
    社長PCの常駐ワーカー (I:\\AI_Workspace\\subsk-worker) へ転送し、
    Claude Code CLI = サブスク枠 (API課金ゼロ) で処理してもらう。
    ワーカーが不在なら即座に None を返し、呼び出し側は従来どおり
    Anthropic API 直呼び (課金) で処理する。

設計原則 (スタッフのUXを変えない):
    - 画面・ジョブフロー・出力物は一切変更しない。utils.claude_query と
      web_searcher._claude_research_call の「テキスト生成の転送路」だけ差し替え。
    - ワーカー不在 (社長PC停止・ハートビート60秒切れ) → 数秒で API へフォールバック。
    - SUPABASE_URL / SUPABASE_KEY 未設定なら完全に無効 (従来動作のまま)。
    - haiku (検品・多数回の小呼び出し) は API 側に残す設計 → utils.py 側で分岐。

転送路: Supabase Storage (バケット subsk-gateway・非公開)。資料つくーる版
(suasiteam-projects/資料収集系/資料つくーる/scripts/subsk_gateway.py) と同一プロトコル。

環境変数:
    SUPABASE_URL / SUPABASE_KEY  … 転送路 (otona-manabi-tv と同じ Supabase)
    SUBSK_GW=0                   … 明示的に無効化 (ロールバック用)
    SUBSK_GW_TIMEOUT             … ワーカー応答待ちの上限秒 (既定 420)
"""
from __future__ import annotations

try:
    from . import subscription_runtime as _subscription
except ImportError:
    import subscription_runtime as _subscription


import json
import os
import time
import urllib.error
import urllib.request
import uuid

BUCKET = "subsk-gateway"
HEARTBEAT_FRESH_SEC = 60
POLL_INTERVAL_SEC = 2.5
DEFAULT_TIMEOUT_SEC = int(os.environ.get("SUBSK_GW_TIMEOUT", "420"))


def _conf() -> "tuple[str, str] | None":
    if os.environ.get("SUBSK_GW", "1").strip() == "0":
        return None
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        return None
    return url, key


def _storage(method: str, path: str, body=None, headers=None, timeout: float = 20.0):
    conf = _conf()
    if not conf:
        raise RuntimeError("gateway disabled")
    url, key = conf
    h = {"Authorization": f"Bearer {key}", "apikey": key}
    if headers:
        h.update(headers)
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(f"{url}{path}", data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _worker_alive() -> bool:
    try:
        status, body = _storage("GET", f"/storage/v1/object/{BUCKET}/hb/worker.json", timeout=10.0)
        if status != 200:
            return False
        hb = json.loads(body.decode("utf-8"))
        return (time.time() - float(hb.get("ts", 0))) <= HEARTBEAT_FRESH_SEC
    except Exception:
        return False


WORKER_RETRY_WINDOW_SEC = 30   # 不在に見えても復帰をこの秒数まで待つ (ハートビート15秒毎×2周+余裕)
WORKER_RETRY_POLL_SEC = 7.5


def _worker_alive_with_retry() -> bool:
    """ワーカー不在に見えても最大 WORKER_RETRY_WINDOW_SEC 秒は復帰を待ってから判定する。

    ワーカーのハートビートはポーリング処理のネットワーク遅延で一時的に途切れる
    ことがあり (2026-08-25/27 の API 課金の真因)、即断すると稼働中なのに
    API 直呼び (課金) へ落ちる。本当に不在 (社長PC停止等) なら30秒余計に
    待つだけで、従来どおり API へフォールバックする。
    """
    if _worker_alive():
        return True
    deadline = time.time() + WORKER_RETRY_WINDOW_SEC
    while time.time() < deadline:
        time.sleep(WORKER_RETRY_POLL_SEC)
        if _worker_alive():
            print("[subsk-gw] ワーカー一時不応答 → 復帰確認 (API直呼び回避)", flush=True)
            return True
    return False


def gateway_generate(
    kind: str,
    system: str,
    query: str,
    max_tokens: int,
    max_uses: int = 0,
    model: str = "claude-sonnet-5",
    tool: str = "sentence",
    channel: str = "",
) -> "str | None":
    return _subscription.generate(system, query, model=model, use_search=(kind=="research"),
        tool=tool, channel=channel, timeout=1800, max_tokens=max_tokens)[0]
