#!/usr/bin/env python3
"""quote_searcher.py — Commons に無い写真を、引用で使う前提で公式サイト・百科事典・報道から探す。

2026-09-28 社長「Wikimedia Commons だけだと使える範囲が少ないかもね。引用で使用する前提でもう少し範囲を
広げたら編集しやすくなりそう」。カラクリ経済学の Web 素材は Commons（自由に使えるライセンス）だけで、
9/27 の回は採用0件だった。

やり方（Commons で当てはまる写真が無かった文だけ）:
1. web_searcher の一次情報向けの検索（公式サイト・企業IR・官公庁・百科事典・報道を優先）で候補のページを探す
2. 素材サイト（有料の写真素材）・SNS・通販・個人ブログ・まとめのページは使わない（引用として扱いにくい）
3. ページの主画像（Wikipedia は記事の画像・ほかは og:image）を取り、小さすぎる画像は使わない
4. 写っているものを判定役に確かめる（commons_searcher.photo_shows_topic）。ロゴやバナーだけの画像も外す
5. 採用した写真は license「引用」、出典（サイト名・URL）を行と credits.txt に残す

引用として使う条件（編集の時に守る）: 画面に出典（サイト名）を表示する・本編の説明が主で写真は従・
写真を改変しない・その場面で写真を見せる必要があること。
"""

import io
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

QUOTE_LICENSE = "引用"
MIN_W, MIN_H = 480, 270
_UA = "Mozilla/5.0 (compatible; sentence-tsukuru/1.0; +https://sentence.apprendre.jp)"

# 引用の元にしないサイト（写真素材の販売・SNS・通販・個人ブログ・まとめ・動画）
BLOCKED_HOSTS = (
    "gettyimages", "shutterstock", "istockphoto", "stock.adobe", "adobestock", "pixta", "alamy", "123rf",
    "dreamstime", "depositphotos", "photolibrary", "amanaimages", "aflo.com", "imagenavi", "photo-ac",
    "pinterest", "twitter.com", "x.com", "instagram.com", "facebook.com", "tiktok.com", "threads.net",
    "youtube.com", "youtu.be", "nicovideo", "amazon.", "rakuten.", "mercari", "yahoo-shopping",
    "shopping.yahoo", "ameblo.jp", "hatenablog", "hatena.ne.jp", "note.com", "fc2.com", "livedoor.blog",
    "blog.goo.ne.jp", "seesaa", "matome", "naver", "flickr.com", "imgur.com",
)


def host_of(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def is_blocked(url: str) -> bool:
    host = host_of(url)
    return not host or any(b in host for b in BLOCKED_HOSTS)


def _fetch(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(8_000_000)


def _big_enough(data: bytes) -> bool:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
        return w >= MIN_W and h >= MIN_H
    except Exception:
        return False


def _page_image(page_url: str) -> str:
    from web_searcher import _page_main_image, _wikipedia_image_url
    if "wikipedia.org/wiki/" in page_url:
        return _wikipedia_image_url(page_url)
    return _page_main_image(page_url)


def find_quote_photo(client, sel: dict, search=None, judge=None, log: Optional[Callable] = None) -> dict:
    """1件分: 候補ページから、写っているものを確かめた写真を1枚返す（無ければ空）。"""
    from commons_searcher import photo_shows_topic
    from web_searcher import _source_type, search_single_sentence
    log = log or (lambda *a, **k: None)
    search = search or (lambda s: search_single_sentence(client, s, profile="primary_media"))
    judge = judge or (lambda s, cand, data: photo_shows_topic(client, s, cand, data=data))
    try:
        found = search(sel) or {}
    except Exception as e:
        log("websearch", f"引用 №{sel.get('no')} 検索に失敗（{type(e).__name__}）")
        return {}
    pages = []
    if found.get("source_url"):
        pages.append({"url": found["source_url"], "title": found.get("source_title", "")})
    pages += [u for u in (found.get("all_urls") or []) if u.get("url")]
    tried = set()
    for page in pages:
        url = page["url"]
        if url in tried or len(tried) >= 5:
            continue
        tried.add(url)
        if is_blocked(url):
            log("websearch", f"引用 №{sel.get('no')} {host_of(url)} は使わないサイト（素材販売・SNS・通販・個人ブログ等）")
            continue
        try:
            image_url = _page_image(url)
            data = _fetch(image_url) if image_url else b""
        except Exception:
            continue
        if not data or not _big_enough(data):
            continue
        cand = {"thumb_url": image_url, "title": page.get("title") or host_of(url)}
        ok, what = judge(sel, cand, data)
        if ok:
            return {
                "no": sel["no"], "thumb_url": image_url, "source_url": url,
                "source_title": page.get("title") or host_of(url), "topic": sel.get("topic", ""),
                "license": QUOTE_LICENSE, "license_url": "", "attribution": host_of(url),
                "commons_page_url": "", "source_type": "引用・" + _source_type(url, page.get("title", "")),
                "photo_note": what or "",
            }
        note = f"写っているもの: {what}" if ok is False else what
        log("websearch", f"引用 №{sel.get('no')} {host_of(url)} の画像は不採用（{note}）")
    return {}


def run_quote_search_for_selections(client, selections: list, max_workers: int = 3,
                                    log: Optional[Callable] = None,
                                    item_callback: Optional[Callable] = None) -> dict:
    """Commons で見つからなかった文を、引用の写真で探す。戻り値 {no: info}。"""
    log = log or (lambda *a, **k: None)
    cb = item_callback or (lambda info: None)
    results = {}
    if not selections:
        return results
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as ex:
        futs = {ex.submit(find_quote_photo, client, s, log=log): s for s in selections}
        for f in as_completed(futs):
            s = futs[f]
            try:
                info = f.result()
            except Exception as e:
                log("websearch", f"引用 №{s.get('no')} 失敗（{type(e).__name__}）")
                info = {}
            if info:
                results[s["no"]] = info
                cb(info)
                log("websearch", f"引用 №{s['no']} {info['attribution']} の写真を採用（出典の表示が必要）")
    log("websearch", f"引用: {len(results)}/{len(selections)} 件取得（公式・百科事典・報道から・写っているものを確認）")
    return results
