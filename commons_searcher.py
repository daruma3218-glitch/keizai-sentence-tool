#!/usr/bin/env python3
"""commons_searcher.py (v3 Step3) — Wikimedia Commons 限定の画像取得

2026-06-12 安福: 収益化チャンネルの権利リスク対策。web_photo の取得元を
Wikimedia Commons API に限定し、**許可ライセンスのみ採用**＋ライセンス/クレジットを
構造化記録する。CC BY 系のクレジット表記義務に対応するため credits.txt も出す。

- API: commons.wikimedia.org/w/api.php (generator=search + imageinfo + extmetadata)
- 採用条件: LicenseShortName が許可リスト(Public domain / CC0 / CC BY / CC BY-SA)のみ。
  非営利(NC)・改変禁止(ND)は除外。
- 日本語クエリで 0 件なら英訳して再検索（Claude 小呼び出し・バッチ）。
- run_web_search_for_selections と同じ item_callback(info) 形で結果を返す
  （info に license / license_url / attribution / commons_page_url を追加）。

2026-09-28 社長の試験（ルノアール回 №46「喫茶店業界を大きく変えたのがドトールでした」）で、
19世紀の本（カモンイス『ルシアダス』）の表紙のスキャンが採用された。Commons の検索は本のスキャン
（DjVu）の中の文字まで探し、「Doutor」（ポルトガル語で博士）に当たった。検索結果の先頭を
ライセンスと大きさだけで採用していたため、写っているものを確かめていなかった。
→ 写真（JPEG・PNG・WebP）だけを検索し（filetype:bitmap）、候補を最大 JUDGE_MAX 枚まで AI に見せて
  「探しているものそのものが写っているか」を確かめ、写っているものだけを使う。
  どれも当てはまらなければ採用しない（呼び出し側が世界観のイラストで代わりを作る）。
"""

import json
import re
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from utils import claude_query, parse_json_object

# 写っているものを確かめる候補の数（1回の検索あたり・1枚15〜25秒）と、1件あたりの上限
JUDGE_MAX = 4
JUDGE_BUDGET = 6
# 写真として使う形式（本・文書のスキャンの DjVu・TIFF・PDF は使わない）
_PHOTO_MIMES = ("image/jpeg", "image/png", "image/webp")

_API = "https://commons.wikimedia.org/w/api.php"
# Wikimedia の User-Agent の決まり（連絡先の入った名乗り）。9/28 本番で連絡先が「noreply」の名乗りのまま
# 1秒に約10件の検索を送り、全部が HTTP 429（Too Many Requests）で断られていた（Commons 0/3 件の原因）。
_UA = ("SentenceTsukuru/1.0 (https://sentence.apprendre.jp/; educational video production by apprendre Inc.) "
       "Python-urllib")
WIKIMEDIA_MIN_INTERVAL = 1.0   # Wikimedia への要求は1秒に1件まで（全スレッド共通）
_wm_lock = threading.Lock()
_wm_last = [0.0]


def wikimedia_get(url: str, timeout: int = 20, retries: int = 2) -> bytes:
    """Wikimedia（API・画像）を1秒に1件までで取得する。429 は Retry-After を待って取り直す。"""
    import time
    import urllib.error
    for attempt in range(retries + 1):
        with _wm_lock:
            wait = _wm_last[0] + WIKIMEDIA_MIN_INTERVAL - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _wm_last[0] = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt >= retries:
                raise
            try:
                pause = float(e.headers.get("Retry-After") or 5)
            except (TypeError, ValueError):
                pause = 5.0
            time.sleep(min(max(pause, 2.0), 15.0))
    raise RuntimeError("unreachable")

# 許可ライセンス（LicenseShortName を小文字化して判定）。NC/ND は除外。
def _license_ok(short_name: str) -> bool:
    s = (short_name or "").strip().lower()
    if not s:
        return False
    if "-nc" in s or "-nd" in s or "noncommercial" in s:
        return False
    if s.startswith("cc0"):
        return True
    if s.startswith("cc by"):  # CC BY / CC BY-SA（NC/ND は上で除外済み）
        return True
    if "public domain" in s or s in ("pd", "pdm", "cc-pd-mark"):
        return True
    return False


def _strip_html(v: str) -> str:
    v = re.sub(r"<[^>]+>", "", v or "")
    return re.sub(r"\s+", " ", v).strip()


def _clean_attribution(ext: dict) -> str:
    """Artist + Credit を整形（HTML除去・重複排除）してクレジット文字列にする。"""
    def field(k):
        return _strip_html((ext.get(k) or {}).get("value", ""))
    parts = [p for p in (field("Artist"), field("Credit")) if p]
    # 重複・極端に長いものを抑制
    seen, out = set(), []
    for p in parts:
        p = p[:120]
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return " / ".join(out) or "Wikimedia Commons"


def search_commons_one(query: str, limit: int = 12, timeout: int = 20,
                       min_w: int = 400, min_h: int = 300) -> Optional[dict]:
    """Commons を検索し、許可ライセンスの画像 1 枚のメタを返す（無ければ None）。

    min_w/min_h: 採用する最小画像サイズ。既定(400x300)で0件のとき、呼び出し側が
    緩めた値で再検索できる（歴史写真など小さめ素材しか無いトピックの取りこぼし対策）。
    """
    found = search_commons_candidates(query, limit, timeout, min_w, min_h, max_n=1)
    return found[0] if found else None


def search_commons_candidates(query: str, limit: int = 12, timeout: int = 20,
                              min_w: int = 400, min_h: int = 300, max_n: int = JUDGE_MAX,
                              errors: Optional[list] = None) -> list:
    """Commons を検索し、許可ライセンスの写真を検索順に最大 max_n 枚返す。

    errors にリストを渡すと、検索の失敗（通信・API のエラー）を書き足す（9/28 本番で 0/3 件の原因が
    見えなかったため。以前は失敗を黙って「0件」にしていた）。
    """
    q = (query or "").strip()
    if not q:
        return []
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"{q} filetype:bitmap", "gsrnamespace": "6", "gsrlimit": str(limit),
        "prop": "imageinfo", "iiprop": "url|extmetadata|size|mime",
        "iiurlwidth": "1280",
    }
    url = _API + "?" + urllib.parse.urlencode(params)
    try:
        data = json.loads(wikimedia_get(url, timeout=timeout).decode("utf-8"))
    except Exception as e:
        if errors is not None:
            errors.append(f"{type(e).__name__}: {str(e)[:80]}")
        return []
    if errors is not None and data.get("error"):
        errors.append(f"API: {str(data['error'].get('info') or data['error'])[:80]}")
    pages = (data.get("query") or {}).get("pages") or {}
    # 検索順（index）でソート
    items = sorted(pages.values(), key=lambda p: p.get("index", 9999))
    found = []
    for p in items:
        ii_list = p.get("imageinfo") or []
        if not ii_list:
            continue
        ii = ii_list[0]
        mime = ii.get("mime", "")
        if mime not in _PHOTO_MIMES:
            continue  # 写真でないもの（SVG・本や文書のスキャンの DjVu/TIFF 等）は除外
        ext = ii.get("extmetadata") or {}
        short = (ext.get("LicenseShortName") or {}).get("value", "")
        if not _license_ok(short):
            continue
        w, h = ii.get("width", 0) or 0, ii.get("height", 0) or 0
        if w < min_w or h < min_h:
            continue  # 小さすぎ（ロゴ・アイコン）を除外
        found.append({
            "url": ii.get("url", ""),
            "thumb_url": ii.get("thumburl") or ii.get("url", ""),
            "license": short,
            "license_url": (ext.get("LicenseUrl") or {}).get("value", ""),
            "attribution": _clean_attribution(ext),
            "commons_page_url": ii.get("descriptionurl", ""),
            "title": (p.get("title", "") or "").replace("File:", ""),
        })
        if len(found) >= max_n:
            break
    return found


PHOTO_JUDGE_SYSTEM = "あなたは動画素材の写真の確認係です。写真を見て、指示された JSON だけを返します。"

PHOTO_JUDGE_QUERY = """この写真を、動画の次の文の場面に使えるかを確かめます。
文: {sentence}
探しているもの: {topic}（検索語: {query}）
写真のファイル名: {title}

写真に「探しているもの」（その店・その会社やブランドの店舗や看板や店内・その人・その場所・その物）が
写っていれば match を true にしてください。
文が特定の店舗や時代（1号店・創業当時など）を指していて、写真が同じ会社・ブランドの別の店舗や今の姿のときも
match は true にし、exact を false にしてください（編集の人が注記を見て使うかを決めます）。
本の表紙・文書や新聞や地図のスキャン・ロゴやバナーや文字だけの画像・無関係の物や場所・同じ名前の別の物・
判別できない写真は match を false。
JSON のみ: {{"match": true または false, "exact": true または false, "what": "写っているものを短く"}}"""


def photo_shows_topic(client, sel: dict, cand: dict, timeout: int = 20, data: bytes = None) -> tuple:
    """(使えるか, 写っているもの)。写真を取れない・判定できないときは (None, 理由)。"""
    from verifier import _encode_for_review, CLAUDE_MODEL
    try:
        if data is None:
            data = wikimedia_get(cand.get("thumb_url") or cand.get("url", ""), timeout=timeout)
        b64, media = _encode_for_review(data, ".jpg", max_side=768)
    except Exception as e:
        return None, f"写真を取得できず（{type(e).__name__}）"
    text = PHOTO_JUDGE_QUERY.format(sentence=(sel.get("sentence") or sel.get("topic") or "")[:200],
                                    topic=sel.get("topic", ""), query=sel.get("query", ""),
                                    title=cand.get("title", "")[:120])
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=300, system=PHOTO_JUDGE_SYSTEM,
            timeout=180, effort="high", workload="assets_review",
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}},
                {"type": "text", "text": text}]}],
        )
        answer = "".join(getattr(b, "text", "") for b in response.content if hasattr(b, "text"))
        obj = parse_json_object(answer)
    except Exception as e:
        return None, f"判定できず（{type(e).__name__}）"
    if not isinstance(obj, dict) or not isinstance(obj.get("match"), bool):
        return None, "判定を読めず"
    what = str(obj.get("what") or "")[:40]
    if obj["match"] and obj.get("exact") is False:
        # 9/28 本番: 「原宿1号店の開業当時とは確認できない」ドトールの店舗まで外していた。同じ会社・ブランドの
        # 写真は使える素材として残し、注記を付ける
        what = "※文の店舗・時代そのものではない可能性: " + what
    return obj["match"], what


def _translate_queries(client, queries: list, log=None) -> dict:
    """日本語クエリを Commons 検索用の英語に一括翻訳（0件時の再検索用）。"""
    log = log or (lambda *a, **kw: None)
    queries = [q for q in dict.fromkeys(queries) if q]
    if not queries or client is None:
        return {}
    listing = "\n".join(f"- {q}" for q in queries)
    system = "あなたは画像検索クエリの翻訳係です。JSON オブジェクトのみ返す。"
    query = f"""次の日本語の画像検索クエリを、Wikimedia Commons 検索に適した簡潔な英語（固有名詞は英語表記）に訳してください。

対象:
{listing}

出力は JSON オブジェクトのみ:
{{"<日本語クエリ>": "<English query>", ...}}"""
    try:
        text = claude_query(client, query, system, max_tokens=2000)
        obj = parse_json_object(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def run_commons_search_for_selections(
    client, selections: list, max_workers: int = 8,
    log: Optional[Callable] = None, item_callback: Optional[Callable] = None,
) -> dict:
    """各 selection(query) で Commons を検索。日本語0件は英訳して再検索。

    item_callback(info) に結果を渡す。info = {no, thumb_url, source_url, source_title,
    topic, license, license_url, attribution, commons_page_url}。
    戻り値: {no: info}（採用できたもののみ）。
    """
    log = log or (lambda *a, **kw: None)
    cb = item_callback or (lambda info: None)
    if not selections:
        return {}

    def _emit(sel, res):
        info = {
            "no": sel["no"],
            "thumb_url": res["thumb_url"],
            "source_url": res["commons_page_url"],
            "source_title": res["title"],
            "topic": sel.get("topic", ""),
            "license": res["license"],
            "license_url": res["license_url"],
            "attribution": res["attribution"],
            "commons_page_url": res["commons_page_url"],
            "photo_note": res.get("photo_note", ""),
        }
        cb(info)
        return info

    judged = {}  # {no: 見せた候補の題}（同じ写真を何度も判定しない）

    def _find(sel, query, min_w=400, min_h=300):
        """検索して、写っているものを確かめた最初の候補を返す（判定役が無いときは先頭）。"""
        seen = judged.setdefault(sel["no"], set())
        errs = []
        cands = search_commons_candidates(query, 12, 20, min_w, min_h, max_n=JUDGE_MAX if client else 1,
                                          errors=errs)
        if errs:
            log("websearch", f"Commons №{sel.get('no')} 検索エラー（{query[:30]}）: {errs[0]}")
        for cand in cands:
            if client is None:
                return cand
            if cand.get("title") in seen or len(seen) >= JUDGE_BUDGET:
                continue
            seen.add(cand.get("title"))
            ok, what = photo_shows_topic(client, sel, cand)
            if ok:
                return {**cand, "photo_note": what}
            note = f"写っているもの: {what}" if ok is False else what
            log("websearch", f"Commons №{sel.get('no')} 「{cand.get('title', '')[:40]}」は不採用（{note}）")
        return None

    def _rest(sel, en):
        """元の検索語で見つからなかった文を、英訳 → 大きさを緩める → 語を後ろから減らす、の順に探す。

        9/28 試験: ルーターの検索語（「ドトールコーヒー 原宿 1号店」など30字まで）は Commons では
        全部の語が合う写真が無く0件になりやすい。短くした語でも、写っているものは判定役が確かめる。
        """
        q = sel.get("query", "")
        steps = []
        if en and en != q:
            steps.append((en, 400, 300))
        steps.append((en or q, 240, 160))
        for base in dict.fromkeys(x for x in (en, q) if x):
            steps += [(short, 400, 300) for short in shorter_queries(base)]
        for query, w, h in dict.fromkeys(steps):
            res = _find(sel, query, w, h)
            if res:
                return res, query
        return None, ""

    results = {}
    pending = []
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as ex:
        futs = {ex.submit(_find, s, s.get("query", "")): s for s in selections}
        for f in as_completed(futs):
            s = futs[f]
            try:
                res = f.result()
            except Exception as e:
                log("websearch", f"Commons №{s.get('no')} 検索に失敗（{type(e).__name__}）")
                res = None
            if res:
                results[s["no"]] = _emit(s, res)
            else:
                pending.append(s)

    later_hits = 0
    if pending:
        trans = _translate_queries(client, [s.get("query", "") for s in pending], log)
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as ex:
            futs = {ex.submit(_rest, s, (trans.get(s.get("query", "")) or "").strip()): s for s in pending}
            for f in as_completed(futs):
                s = futs[f]
                try:
                    res, used = f.result()
                except Exception as e:
                    log("websearch", f"Commons №{s.get('no')} 検索に失敗（{type(e).__name__}）")
                    res, used = None, ""
                if res:
                    results[s["no"]] = _emit(s, res)
                    later_hits += 1
                    log("websearch", f"Commons №{s['no']} 「{res.get('title', '')[:40]}」を採用（検索語: {used}）")
                else:
                    en = (trans.get(s.get("query", "")) or "").strip()
                    log("websearch", f"Commons №{s['no']} 当てはまる写真なし（検索語: {s.get('query', '')}"
                                     + (f" / {en}" if en else "") + "）")

    log("websearch",
        f"Commons: {len(results)}/{len(selections)} 件取得（許可ライセンスのみ・写っているものを確認・"
        f"英訳・語を減らした再検索含む" + (f"・再検索で+{later_hits}" if later_hits else "") + "）")
    return results


SUGGEST_QUERY_PROMPT = """動画の次の文の場面に使う写真を、Wikimedia Commons で探します。
文: {sentence}
前後の段落: {context}

文が指している実在の物（店・会社・建物・場所・人・商品）を前後の段落から特定し、
Commons で探す英語の検索語（固有名詞を先頭に、3〜5語）と、探しているもの（日本語で短く）を答えてください。
JSON のみ: {{"query": "英語の検索語", "topic": "探しているもの"}}"""


def suggest_query(client, sentence: str, context: str = "") -> dict:
    """文と前後の段落から Commons の検索語を決める（1枚の作り直しで、ルーターの検索語が無いとき）。

    9/28: 「1980年4月18日、東京・原宿に、立ち飲みスタイルの1号店を開きます。」は文に店名が無く、
    文をそのまま検索しても写真が見つからなかった（店名＝ドトールは前の文にある）。
    """
    try:
        text = claude_query(client, SUGGEST_QUERY_PROMPT.format(sentence=(sentence or "")[:200],
                                                                context=(context or "")[:600]),
                            "あなたは画像検索の担当です。JSON オブジェクトのみ返す。", max_tokens=300)
        obj = parse_json_object(text)
    except Exception:
        return {}
    if not isinstance(obj, dict) or not str(obj.get("query") or "").strip():
        return {}
    return {"query": str(obj["query"]).strip()[:80], "topic": str(obj.get("topic") or "").strip()[:30]}


def shorter_queries(query: str) -> list:
    """「ドトールコーヒー 原宿 1号店」→ ["ドトールコーヒー 原宿", "ドトールコーヒー"]（語を後ろから減らす）。"""
    words = (query or "").split()
    return [" ".join(words[:i]) for i in range(len(words) - 1, 0, -1)]


def build_credits_text(items: list) -> str:
    """採用した Commons 画像のクレジット一覧（概要欄貼り付け用）を組み立てる。

    items: [{title, attribution, license, commons_page_url}, ...]
    """
    lines = [
        "■ 画像クレジット（Wikimedia Commons）",
        "本動画で使用した画像のライセンス・出典です。",
        "",
    ]
    seen = set()
    n = 0
    for it in items:
        page = (it.get("commons_page_url") or "").strip()
        lic = (it.get("license") or "").strip()
        if not page or not lic:
            continue
        key = page
        if key in seen:
            continue
        seen.add(key)
        n += 1
        title = (it.get("title") or it.get("source_title") or "").strip() or "(file)"
        attr = (it.get("attribution") or "").strip()
        lines.append(f"{n}. {title}")
        if attr:
            lines.append(f"   作者/クレジット: {attr}")
        lines.append(f"   ライセンス: {lic}")
        lines.append(f"   出典: {page}")
        lines.append("")
    if n == 0:
        lines.append("（Wikimedia Commons の画像は使用していません）")
    # 引用で使う写真（2026-09-28 quote_searcher）: 画面に出典を表示し、概要欄にも載せる
    quoted, seen_q = [], set()
    for it in items:
        if (it.get("license") or "").strip() != "引用":
            continue
        url = (it.get("source_url") or "").strip()
        if not url or url in seen_q:
            continue
        seen_q.add(url)
        quoted.append(it)
    if quoted:
        lines += ["", "■ 引用した画像の出典（画面にも出典を表示してください）", ""]
        for i, it in enumerate(quoted, 1):
            lines.append(f"{i}. {(it.get('source_title') or it.get('attribution') or '').strip()}（№{it.get('no')}）")
            lines.append(f"   出典: {(it.get('attribution') or '').strip()} {it.get('source_url')}")
            lines.append("")
    return "\n".join(lines)
