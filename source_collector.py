#!/usr/bin/env python3
"""source_collector.py — 元ネタ動画・参考動画の収集（制作資料パック用）

章ごとに Claude の Web 検索で「内容の元ネタ・参考になり得る実在の動画」
（本人の講演・インタビュー・対談・公式チャンネル・ドキュメンタリー等）を探して
一覧化する。成功の法則のような「実在の人物・企業・研究」を扱うチャンネルで、
編集者が裏取り・演出参考にできる資料を作るのが目的。

※ここで集めた動画 URL は動画内の素材には使わない（資料・裏取り用）。
  画像収集側（web_searcher の primary_media プロファイル）の YouTube 除外
  ポリシーはそのまま維持される。
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from utils import parse_json_array
from web_searcher import _claude_research_call


def collect_chapter_source_videos(client, title: str, chapter_title: str,
                                  chapter_digest: str, per_chapter: int = 3,
                                  timeout: float = 75.0) -> list:
    """1章ぶんの元ネタ動画候補を Web 検索で集める（最大 per_chapter 件）。"""
    system = (
        "あなたは動画制作のリサーチャーです。章の内容の『元ネタ・参考になる実在の動画』を"
        "Web検索で探します。結果は必ず JSON 配列のみで返してください。"
    )
    query = f"""動画「{title}」の章「{chapter_title}」の元ネタ・参考になる動画を Web 検索で探してください。

章の内容（冒頭の要約）:
{(chapter_digest or "")[:600]}

【探すもの】本人の講演・インタビュー・対談、公式チャンネルの動画、ドキュメンタリー、
大学の講義など、内容の裏取りや演出の参考になる実在の動画（YouTube / TED / Vimeo 可）
【海外の一次情報を優先】対象が海外の人物・企業・研究なら、**英語でも検索**して
原語の一次情報（TED Talk、本人の英語スピーチ・インタビュー、公式チャンネル、
大学講義、カンファレンス登壇）を優先的に含めること。日本語の解説・要約動画より原典を上位に。
【避けるもの】無関係なまとめ動画、転載と思われるもの、内容の薄いショート動画
最大 {per_chapter} 件。確信が持てるものだけ。見つからなければ空配列 [] を返す。
title は原語のままでよい。reason は日本語で書く。

【出力 JSON のみ・前置き禁止】
[
  {{"url": "https://...", "title": "動画タイトル", "reason": "元ネタ/参考になる理由（30字以内）"}}
]"""
    # max_uses=4: 日本語と英語（原語）の両方で検索できる余地を持たせる
    text, real_urls = _claude_research_call(
        client, query, system, max_tokens=1500, max_uses=4, timeout=timeout)
    items = parse_json_array(text)
    real = [u.get("url", "") for u in real_urls if u.get("url")]
    out = []
    for it in items:
        if len(out) >= per_chapter:
            break
        if not isinstance(it, dict):
            continue
        u = (it.get("url") or "").strip()
        if not u.lower().startswith(("http://", "https://")):
            continue
        # 検索結果に実在した URL か（ハルシネーションURLの目印にする。除外はしない）
        verified = any(u == r or u.startswith(r) or r.startswith(u) for r in real)
        out.append({
            "url": u,
            "title": (it.get("title") or "")[:100],
            "reason": (it.get("reason") or "")[:80],
            "verified": bool(verified),
        })
    return out


def collect_source_videos(client, title: str, chapters: list, rows: list,
                          per_chapter: int = 3, max_chapters: int = 10,
                          max_workers: int = 3,
                          log: Optional[Callable] = None) -> dict:
    """全章の元ネタ動画を並列収集する。戻り値: {chapter_index: [video, ...]}。

    章のダイジェストは各章の先頭数文をつなげたもの（Claudeに文脈を渡すため）。
    失敗した章は空のまま進める（資料はベストエフォート・ジョブは止めない）。
    """
    log = log or (lambda *a, **kw: None)
    by_ch = {}
    for r in rows:
        ci = r.get("chapter_index")
        if ci is None:
            continue
        by_ch.setdefault(ci, []).append(r.get("sentence", ""))

    jobs = []
    for ci, ch in enumerate(chapters[:max_chapters]):
        digest = " ".join(by_ch.get(ci, [])[:6])
        jobs.append((ci, ch.get("title", f"第{ci + 1}章"), digest))
    if len(chapters) > max_chapters:
        log("sources", f"元ネタ動画検索は先頭 {max_chapters} 章まで（全 {len(chapters)} 章）")

    results = {}
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as ex:
        futs = {
            ex.submit(collect_chapter_source_videos, client, title, t, d, per_chapter): (ci, t)
            for ci, t, d in jobs
        }
        for f in as_completed(futs):
            ci, t = futs[f]
            try:
                results[ci] = f.result()
            except Exception as e:
                log("sources", f"元ネタ動画検索失敗（{t}）: {str(e)[:60]}")
                results[ci] = []
    total = sum(len(v) for v in results.values())
    log("sources", f"元ネタ動画: {total} 本を収集（{len(jobs)} 章・裏取り/演出参考用）")
    return results
