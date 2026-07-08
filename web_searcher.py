#!/usr/bin/env python3
"""Phase 2b: Web 画像 URL 取得（Claude Web Search 経由）

センテンスから「歴史人物・地名・固有事件」を検出し、
Wikipedia 等のソース URL とサムネイル画像 URL を取得する。

ユーザーはこの URL を見て手動で画像を選定・ダウンロードする想定。
"""

import json
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from typing import Callable, Optional

import anthropic

from utils import cached_system_param, cached_user_content, log_prompt_cache_usage, parse_json_array


CLAUDE_MODEL = "claude-sonnet-5"
SEARCH_BATCH_SIZE = 6  # 1 リクエストで複数センテンスをまとめて検索


def _claude_research_call(
    client,
    query: str,
    system: str,
    max_tokens: int = 4096,
    max_uses: int = 5,
    timeout: float = 60.0,
) -> tuple:
    """Claude Web Search を実行して (text, real_urls) を返す"""
    real_urls = []
    text_parts = []
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=cached_system_param(system),
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": max_uses,
            }],
            messages=[{"role": "user", "content": query}],
            timeout=timeout,
        )
        log_prompt_cache_usage(response, "web_search")
        for block in response.content:
            block_type = getattr(block, "type", "")
            if hasattr(block, "text"):
                text_parts.append(block.text)
            if block_type == "web_search_tool_result":
                content = getattr(block, "content", [])
                for r in content:
                    if getattr(r, "type", "") == "web_search_result":
                        u = getattr(r, "url", "")
                        t = getattr(r, "title", "")
                        if u:
                            real_urls.append({"url": u, "title": t})
    except Exception as e:
        print(f"  [WebSearch ERROR] {e}", flush=True)
    return "\n".join(text_parts), real_urls


def _wikipedia_image_url(article_url: str) -> str:
    """Wikipedia 記事 URL からサムネイル画像 URL を取得（公式 API）

    例: https://ja.wikipedia.org/wiki/ヨシフ・スターリン
        → https://...wikipedia/commons/thumb/.../Stalin.jpg/220px-Stalin.jpg
    """
    try:
        m = re.match(r'https?://([a-z]+)\.wikipedia\.org/wiki/(.+)', article_url)
        if not m:
            return ""
        lang = m.group(1)
        title = urllib.parse.unquote(m.group(2))
        api = f"https://{lang}.wikipedia.org/w/api.php"
        params = {
            "action": "query",
            "titles": title,
            "prop": "pageimages",
            "format": "json",
            "pithumbsize": "400",
            "redirects": "1",
        }
        url = f"{api}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "sentence-tool/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        pages = data.get("query", {}).get("pages", {})
        for _pid, pg in pages.items():
            thumb = pg.get("thumbnail", {})
            if thumb.get("source"):
                return thumb["source"]
    except Exception as e:
        print(f"  [Wikipedia thumb ERROR] {e}", flush=True)
    return ""


# og:image / twitter:image を拾う（属性順が逆のHTMLにも対応）
_META_IMAGE_PATTERNS = [
    re.compile(
        r'<meta[^>]+(?:property|name)\s*=\s*["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\'][^>]*\scontent\s*=\s*["\']([^"\']+)["\']',
        re.IGNORECASE),
    re.compile(
        r'<meta[^>]+content\s*=\s*["\']([^"\']+)["\'][^>]*\s(?:property|name)\s*=\s*["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\']',
        re.IGNORECASE),
]


def _extract_og_image(html: str, base_url: str = "") -> str:
    """HTML から og:image / twitter:image の画像URLを取り出す（相対URLは絶対化）。"""
    if not html:
        return ""
    for pat in _META_IMAGE_PATTERNS:
        m = pat.search(html)
        if not m:
            continue
        img = (m.group(1) or "").strip()
        if not img:
            continue
        if img.startswith("//"):
            img = "https:" + img
        elif base_url and not img.lower().startswith(("http://", "https://")):
            img = urllib.parse.urljoin(base_url, img)
        if img.lower().startswith(("http://", "https://")):
            return img
    return ""


def _page_main_image(page_url: str, timeout: int = 8) -> str:
    """記事ページの主画像（og:image）URLを返す。無ければ空文字。

    Wikipedia 以外（報道・公的機関・公式サイト等）からも参考画像を取れるようにする。
    メタタグは <head> にあるためページ先頭 200KB だけ読む（軽量・安全）。
    """
    if not page_url or not page_url.lower().startswith(("http://", "https://")):
        return ""
    try:
        req = urllib.request.Request(page_url, headers={"User-Agent": "sentence-tool/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if ctype and "html" not in ctype:
                return ""
            html = resp.read(200_000).decode("utf-8", errors="ignore")
        return _extract_og_image(html, base_url=page_url)
    except Exception:
        return ""


def _youtube_thumbnail_url(url: str) -> str:
    """YouTube URL から標準サムネイルURLを推定する。"""
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()
        vid = ""
        if "youtu.be" in host:
            vid = parsed.path.strip("/").split("/")[0]
        elif "youtube.com" in host:
            qs = urllib.parse.parse_qs(parsed.query)
            vid = (qs.get("v") or [""])[0]
            if not vid and parsed.path.startswith("/shorts/"):
                vid = parsed.path.split("/")[2]
        if vid:
            return f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
    except Exception:
        pass
    return ""


def _is_youtube_url(url: str) -> bool:
    u = (url or "").lower()
    return "youtube.com" in u or "youtu.be" in u


def _source_type(url: str, title: str = "") -> str:
    u = (url or "").lower()
    t = (title or "").lower()
    if _is_youtube_url(u):
        return "youtube"
    if any(x in u for x in ("ted.com", "vimeo.com", "coursera.org")):
        return "video"
    if any(x in u for x in (".gov", ".go.jp", ".go.", "who.int", "oecd.org", "worldbank.org", "imf.org")):
        return "official"
    if any(x in u for x in (".edu", ".ac.jp", "scholar.google", "researchgate", "ssrn.com", "arxiv.org", "doi.org")):
        return "research"
    if any(x in u for x in ("annualreport", "investor", "ir.", "/ir/", "press-release", "newsroom")):
        return "company"
    if "wikipedia.org" in u:
        return "encyclopedia"
    if any(x in t for x in ("interview", "keynote", "lecture", "登壇", "講演", "インタビュー")):
        return "media"
    return "article"


def _select_chunk(
    client: anthropic.Anthropic,
    candidates: list,
    target_count: int,
    exclude_nos: set,
    log: Callable,
    profile: str = "",
) -> list:
    """1 回の Claude 呼び出しで指定数を選定（チャンク単位）"""
    if target_count <= 0:
        return []

    # 既に選ばれた no は候補から除外
    chunk_candidates = [c for c in candidates if c["no"] not in exclude_nos]
    if not chunk_candidates:
        return []

    inputs_json = json.dumps(chunk_candidates, ensure_ascii=False, indent=2)

    primary_media = profile == "primary_media"
    if primary_media:
        system = (
            "あなたは動画制作向けの素材リサーチャーです。"
            "原稿センテンスから、一次情報・記事・実在人物写真・講演資料・公式資料を"
            "Web検索で探す価値が高いものを選びます。結果は必ず JSON 配列のみで返してください。"
        )
        criteria = """【選定基準（成功の法則向け・一次情報/実写素材を広く採用）】
- 実在人物、著者、起業家、研究者、経営者、講演者、スポーツ選手、アーティスト
- 企業名、商品名、サービス名、ブランド名、実在プロジェクト
- 書籍、論文、研究、統計、調査、大学、研究機関、政府機関
- 公式発表、プレスリリース、年次報告書、IR資料、講演資料
- 大学・企業イベント等の講演ページ、登壇記事、インタビュー記事（YouTube は除外）
- 実際の職場、会議、店舗、製造現場、学校、スポーツ現場などリアル画像が効く文
- 抽象概念でも、具体的な人物・事例・企業・本・研究に結びつけて素材化できる文

【検索クエリ方針】
- 可能なら「公式」「講演」「登壇」「インタビュー」「論文」「統計」
  「年次報告書」「プレスリリース」「大学」「政府」「原典」を含める
- 人物名・企業名・書籍名など固有名詞を優先
- YouTube / youtu.be は検索対象・採用対象から除外

【素材タイプ（material_type）を各候補に必ず付ける】クエリはタイプに合わせて作る:
- scene: 実際の場面・現場（職場・工場・会議・店舗・研究室・イベント会場）
  → クエリ例:「◯◯社 オフィス 様子」「◯◯ 製造現場 写真」「◯◯ カンファレンス 会場」
- person: 実在人物 → クエリ例:「氏名 講演 写真」「氏名 インタビュー 公式」
- document: 書籍・論文・レポート・公式資料 → クエリ例:「書名 表紙」「◯◯ 論文 大学」
- data: 統計・調査データ → クエリ例:「◯◯ 統計 出典 政府」

【海外対象は英語クエリで】対象が海外の人物・企業・書籍・研究なら、query は
**英語で作ってよい**（原語の一次情報のほうが公式写真・原典ページが見つかるため）。
例: "James Clear interview photo" / "Stanford marshmallow experiment" / "Patagonia headquarters office"
"""
    else:
        system = (
            "あなたは動画素材リサーチャーです。"
            "原稿センテンスから Web 検索で参考画像が見つかりそうなものを選びます。"
            "結果は必ず JSON 配列のみで返してください。前置きや説明は不要です。"
        )
        criteria = """【選定基準（広めに採用）】
- 歴史人物名（スターリン、毛沢東、ニクソン、ゴルバチョフ など）
- 固有歴史事件名（アイグン条約、朝鮮戦争、ニクソン訪中、シベリア抑留 など）
- 具体的地名・建造物（ウラジオストク、満州、バイカル湖、紫禁城 など）
- 文書・条約・著書（ヤルタ協定、防共協定、毛沢東語録 など）
- 兵器・物（T-34戦車、原子爆弾、戦闘機、ロケット など）
- 国名・国旗（アメリカ・中国・ロシア・ソ連 など固有の国名）
- 地理的特徴（バイカル湖、シベリア、太平洋 など）
- 統計データの背景となるもの（GDP、軍事費、原油生産 → 関連写真）
"""
    exclude_note = f"\n\n【除外: 以下の no はすでに選定済みなので絶対に選ばないこと】\n{sorted(exclude_nos)[:200]}" if exclude_nos else ""
    # primary_media は素材タイプ（scene/person/document/data）も出力させ、検索クエリの的中率を上げる
    mt_field = ', "material_type": "scene|person|document|data"' if primary_media else ""
    q_lang = "日本語または英語（海外対象は英語推奨）" if primary_media else "日本語"
    # 固定ルール部（プロファイル毎に一定）を先頭に置き prompt cache 対象にする。
    # 件数・センテンス・除外リストなど毎回変わるものは後段の動的部へ分離。
    fixed_selection_rules = f"""以下のセンテンスから、Web で参考画像（写真・絵画・歴史画像）が見つかりやすい候補を指定件数ぶん選んでください。

{criteria}

【選定方針】
- できるだけ多く選ぶ。指定件数に満たない場合は、候補センテンスから関連画像が見つかりそうなものを広く拾う
- 「やや関連がある程度」でも採用してよい

【除外】
- 純粋に抽象的な接続詞・挨拶のみ
- 「では」「次に」だけの内容のない文

【出力 JSON（必ずこの形式のみ）】
[
  {{"no": 元のno, "topic": "短い検索トピック名(10〜30文字)", "query": "Web検索クエリ({q_lang}、40文字以内、固有名詞含む)"{mt_field}}}
]
出力は JSON 配列のみ、前置き禁止。"""

    dynamic_selection_payload = f"""候補センテンス:
{inputs_json}{exclude_note}

必ず可能な限り {target_count} 件を出力。"""

    query = cached_user_content(
        (fixed_selection_rules, True),
        (dynamic_selection_payload, False),
    )

    # 必要 token 数を推算（1 件あたり約 150 token）
    needed_tokens = max(4000, target_count * 200 + 2000)
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=min(needed_tokens, 16000),  # 上限 16k
        system=cached_system_param(system),
        messages=[{"role": "user", "content": query}],
        timeout=90.0,
    )
    log_prompt_cache_usage(response, "websearch.select")
    text = ""
    for b in response.content:
        if hasattr(b, "text"):
            text += b.text

    # デバッグ: stop_reason / token 数
    stop_reason = getattr(response, "stop_reason", "")
    usage = getattr(response, "usage", None)
    out_tokens = getattr(usage, "output_tokens", 0) if usage else 0
    log("websearch",
        f"  chunk: max_tokens={needed_tokens}, 応答 {out_tokens} tokens, stop={stop_reason}")

    parsed = parse_json_array(text)
    if not parsed:
        log("websearch", f"  ⚠ JSON パース失敗。応答先頭: {text[:200]}")
        return []
    return parsed


def select_search_worthy_sentences(
    client: anthropic.Anthropic,
    rows: list,
    target_count: int,
    log: Optional[Callable] = None,
    profile: str = "",
) -> list:
    """Claude で「Web 画像が役立つセンテンス」を target_count 件選ぶ

    target_count が大きい (>30) 場合は内部で複数チャンクに分割して
    トークン制限と「Claude が一度に大量返却を渋る」問題の両方を回避する。
    """
    log = log or (lambda *a, **kw: None)
    if target_count <= 0 or not rows:
        return []

    # 候補センテンス（短文や記号のみを除外）
    candidates = []
    for r in rows:
        sent = r.get("sentence", "")
        if len(sent.strip()) < 8:
            continue
        if not re.search(r'[一-龥ァ-ヴーa-zA-Z]', sent):
            continue
        candidates.append({"no": r["no"], "sentence": sent[:200]})

    if not candidates:
        log("websearch", "候補センテンスが 0 件です（原稿が短すぎる）")
        return []

    log("websearch",
        f"Claude で Web 検索対象を選定中: 候補 {len(candidates)} / 目標 {target_count} 件")

    # チャンク分割: 1 リクエストあたり最大 30 件まで（Claude が大量返却を渋るため）
    CHUNK_SIZE = 30
    no_set = {r["no"] for r in rows}
    all_valid = []
    selected_nos: set = set()
    remaining = target_count
    chunk_idx = 0

    while remaining > 0 and chunk_idx < 10:  # 最大 10 チャンク
        chunk_idx += 1
        request_n = min(remaining, CHUNK_SIZE)
        log("websearch", f"  チャンク {chunk_idx}: {request_n} 件要求（残り {remaining} 件）")

        try:
            selected = _select_chunk(client, candidates, request_n, selected_nos, log, profile=profile)
        except Exception as e:
            log("error", f"  チャンク {chunk_idx} 失敗: {str(e)[:120]}")
            break

        if not selected:
            log("websearch", f"  チャンク {chunk_idx}: 選定 0 件（候補不足の可能性）→ 中断")
            break

        added = 0
        for s in selected:
            if not isinstance(s, dict):
                continue
            no = s.get("no")
            if no not in no_set or no in selected_nos:
                continue
            if not s.get("query"):
                continue
            selected_nos.add(no)
            all_valid.append(s)
            added += 1
            if len(all_valid) >= target_count:
                break

        log("websearch", f"  チャンク {chunk_idx}: {added} 件追加 → 累計 {len(all_valid)}/{target_count}")
        remaining = target_count - len(all_valid)

        # チャンクで何も追加できなかった場合は打ち切り（同じ候補を繰り返すだけ）
        if added == 0:
            break

    log("websearch", f"選定完了: {len(all_valid)} 件（{chunk_idx} チャンク使用）")
    return all_valid[:target_count]


def download_thumbnail(thumb_url: str, output_path) -> bool:
    """Wikimedia 等のサムネイル画像をローカルに保存する。

    参考用の低解像度サムネイル（Wikimedia Commons は大半が CC/PD）を
    動画素材の下調べ用にダウンロードする。
    壊れたURL・極小画像（ロゴ/アイコン）は検証して弾く。
    """
    from pathlib import Path
    from io import BytesIO
    if not thumb_url:
        return False
    output_path = Path(output_path)
    try:
        req = urllib.request.Request(
            thumb_url,
            headers={"User-Agent": "sentence-tool/1.0 (educational video material research)"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            if getattr(resp, "status", 200) != 200:
                return False
            ctype = resp.headers.get("Content-Type", "")
            data = resp.read()
        # Content-Type が画像でない（HTMLエラーページ等）→ 弾く
        if "image" not in ctype.lower():
            return False
        if not data or len(data) < 500:
            return False
        # PIL で実画像か検証 + サイズチェック（極小ロゴ/アイコンを除外）
        try:
            from PIL import Image
            img = Image.open(BytesIO(data))
            img.load()
            w, h = img.size
            if w < 80 or h < 80:
                return False  # 小さすぎ（ロゴ・アイコンの可能性）
        except Exception:
            return False  # 画像として開けない＝壊れURL
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(data)
        return True
    except Exception as e:
        print(f"  [thumb download ERROR] {str(e)[:100]}", flush=True)
        return False


def search_single_sentence(
    client: anthropic.Anthropic,
    selection: dict,
    profile: str = "",
) -> dict:
    """1 センテンスに対する Web 画像 URL を取得"""
    no = selection["no"]
    query_text = selection.get("query", "")
    topic = selection.get("topic", "")

    primary_media = profile == "primary_media"
    if primary_media:
        system = (
            "あなたは動画制作向けの素材リサーチャーです。"
            "記事、公式資料、一次情報、実在人物の写真、講演ページ・登壇記事を探します。"
            "公式サイト、大学・政府・企業IR、論文、プレスリリース、公式プロフィール等を優先してください。"
            "YouTube / youtu.be は検索対象・採用対象から必ず除外してください。"
        )
        priority = (
            "- 公式サイト、企業IR、年次報告書、プレスリリース、政府/大学/研究機関、論文、統計など一次情報を最優先\n"
            "- 実在人物が出る場合は、本人公式サイト・Wikipedia/Wikimedia・公式プロフィール・講演ページを優先\n"
            "- 記事だけでなく、講演ページ・インタビュー記事・登壇資料も候補に含める\n"
            "- 対象が海外の人物・企業・研究なら、**英語でも検索**し、原語の一次情報"
            "（本人公式サイト・TED・大学・海外報道・原典ページ）を積極的に採用する\n"
            "- YouTube / youtu.be のURLは採用しない\n"
            "- ゴシップ、まとめサイト、無断転載、出典不明サムネイルは避ける\n"
        )
        # 選定時に付与された素材タイプで検索の狙いを絞る（場面写真の的中率向上）
        material_type = (selection.get("material_type") or "").strip()
        mt_hints = {
            "scene": "実際の場面・現場の写真が載ったページ（現地レポート・公式の施設/オフィス紹介・イベントレポート等）を最優先",
            "person": "本人が写った写真のある公式プロフィール・講演レポート・インタビュー記事を最優先",
            "document": "書籍の表紙・論文・公式レポートが掲載されたページを最優先",
            "data": "出典の明確な統計・調査データの掲載ページ（政府・研究機関・企業IR）を最優先",
        }
        if material_type in mt_hints:
            priority += f"- 素材タイプ [{material_type}]: {mt_hints[material_type]}\n"
    else:
        system = (
            "あなたはリサーチャーです。Web 検索で指定トピックの参考画像が掲載されたページを探します。"
            "Wikipedia や公的機関、報道機関のサイトを優先してください。"
        )
        priority = (
            "- 画像が含まれるページ（Wikipedia 等）を優先\n"
            "- 公的機関・報道機関・百科事典のサイトを優先\n"
        )

    query = f"""「{topic}」に関する参考素材が載っているページを Web 検索で探してください。
検索クエリ: {query_text}

【最重要】
- 必ず Web 検索を実行すること
- 画像・記事・一次資料・講演ページ・インタビュー記事のうち、動画素材制作に使いやすいものを優先
- YouTube / youtu.be は採用しない
{priority}
- 数件で OK。最も信頼性の高い 1 件を選んで返す

回答後に Web 検索結果のリストもそのまま記述してください。"""

    text, urls = _claude_research_call(client, query, system, max_tokens=2000, max_uses=2, timeout=60.0)

    # 最良の URL を 1 件選ぶ
    best_url = ""
    best_title = ""
    if primary_media:
        # 一次情報・研究/公式資料を優先。YouTube はユーザー方針で除外。
        priority_types = ("official", "research", "company", "video", "encyclopedia", "article")
        for typ in priority_types:
            for u in urls:
                if _is_youtube_url(u.get("url", "")):
                    continue
                if _source_type(u.get("url", ""), u.get("title", "")) == typ:
                    best_url = u["url"]
                    best_title = u["title"]
                    break
            if best_url:
                break
    else:
        for u in urls:
            if _is_youtube_url(u.get("url", "")):
                continue
            # Wikipedia 優先
            if "wikipedia.org" in u["url"]:
                best_url = u["url"]
                best_title = u["title"]
                break
    if not best_url and urls:
        for u in urls:
            if _is_youtube_url(u.get("url", "")):
                continue
            best_url = u["url"]
            best_title = u["title"]
            break

    # サムネイル取得: Wikipedia は公式API、それ以外のサイトは og:image（ページ主画像）。
    # 最良URLで画像が取れなければ他の検索結果も順に試す（取得率と関連性の底上げ）。
    thumb_url = ""
    candidates = []
    if best_url:
        candidates.append({"url": best_url, "title": best_title})
    for u in urls:
        uu = u.get("url", "")
        if not uu or uu == best_url or _is_youtube_url(uu):
            continue
        candidates.append(u)
        if len(candidates) >= 4:
            break
    for cand in candidates:
        cu = cand.get("url", "")
        if not cu:
            continue
        if "wikipedia.org/wiki/" in cu:
            t = _wikipedia_image_url(cu)
        else:
            t = _page_main_image(cu)
        if t:
            thumb_url = t
            if cu != best_url:
                # 実際に画像が取れたページを出典として採用（出典と画像のズレを防ぐ）
                best_url, best_title = cu, cand.get("title", "")
            break

    source_type = _source_type(best_url, best_title)

    return {
        "no": no,
        "topic": topic,
        "query": query_text,
        "source_url": best_url,
        "source_title": best_title,
        "source_type": source_type,
        "material_type": (selection.get("material_type") or ""),  # 資料パックの分類用
        "thumb_url": thumb_url,
        "all_urls": urls[:5],  # 候補も残す
    }


def _run_parallel_searches(
    client: anthropic.Anthropic,
    selections: list,
    max_workers: int,
    log: Callable,
    item_callback: Callable,
    profile: str = "",
) -> list:
    """Web検索を並列実行する。遅い検索が混ざっても全体を止めない。"""
    results = []
    completed = 0
    # 1件60秒を上限にしているため、波数分 + 余裕で全体待ち時間を決める。
    waves = max(1, (len(selections) + max_workers - 1) // max_workers)
    overall_timeout = max(180, min(900, waves * 75))
    executor = ThreadPoolExecutor(max_workers=max_workers)
    future_to_sel = {
        executor.submit(search_single_sentence, client, sel, profile): sel
        for sel in selections
    }
    try:
        try:
            iterator = as_completed(future_to_sel, timeout=overall_timeout)
            for future in iterator:
                sel = future_to_sel[future]
                try:
                    r = future.result()
                    results.append(r)
                    completed += 1
                    log("websearch", f"検索 {completed}/{len(selections)}: 「{r.get('topic', '')[:30]}」")
                    item_callback(r)
                except Exception as e:
                    completed += 1
                    log("error", f"検索エラー（no={sel.get('no')}）: {str(e)[:100]}")
        except FuturesTimeout:
            pending = [sel for fut, sel in future_to_sel.items() if not fut.done()]
            log(
                "warn",
                f"Web検索がタイムアウト: 完了 {completed}/{len(selections)} / 未完了 {len(pending)} 件をスキップして続行"
            )
            for sel in pending[:10]:
                log("warn", f"未完了検索をスキップ no={sel.get('no')} query={str(sel.get('query', ''))[:40]}")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    results.sort(key=lambda x: x.get("no", 0))
    log("websearch", f"Web 検索完了: {len(results)} 件")
    return results


def run_web_search_for_selections(
    client: anthropic.Anthropic,
    selections: list,
    max_workers: int = 8,
    log: Optional[Callable] = None,
    item_callback: Optional[Callable] = None,
    profile: str = "",
) -> list:
    """v2 用: ルーターが選んだ web_photo 文を検索する（選定ステップなし）。

    selections: [{"no": int, "query": str, "topic": str}, ...]
        ルーターの search_query / topic をそのまま使うので、ここでは選定しない。
    """
    log = log or (lambda *a, **kw: None)
    item_callback = item_callback or (lambda info: None)

    if not selections:
        log("websearch", "Web 検索対象（web_photo）がありません")
        return []

    log("websearch", f"{len(selections)} 件の Web 検索を並列実行中（同時 {max_workers}）...")
    return _run_parallel_searches(client, selections, max_workers, log, item_callback, profile)


def run_web_search(
    client: anthropic.Anthropic,
    rows: list,
    target_count: int = 50,
    max_workers: int = 4,
    log: Optional[Callable] = None,
    item_callback: Optional[Callable] = None,
    profile: str = "",
) -> list:
    """エントリポイント: Web 画像 URL を取得して返す（v1 互換: 内部で選定する）

    戻り値:
      [
        {
          "no": int,
          "topic": str,
          "source_url": str,
          "source_title": str,
          "thumb_url": str,
          "all_urls": [{"url": str, "title": str}, ...]
        },
        ...
      ]
    """
    log = log or (lambda *a, **kw: None)
    item_callback = item_callback or (lambda info: None)

    if target_count <= 0:
        return []

    # Step 1: 検索対象センテンスを Claude が選定
    selections = select_search_worthy_sentences(client, rows, target_count, log=log, profile=profile)
    if not selections:
        log("websearch", "Web 検索対象センテンスが見つかりません")
        return []

    # Step 2: 並列に Web 検索を実行
    log("websearch", f"{len(selections)} 件の Web 検索を並列実行中（同時 {max_workers}）...")
    return _run_parallel_searches(client, selections, max_workers, log, item_callback, profile)
