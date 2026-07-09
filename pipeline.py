#!/usr/bin/env python3
"""メインパイプライン: 4 フェーズ統合

Phase 1: 原稿 → 章/ブロック/センテンス分解（Claude）
Phase 2a: センテンス → 英文画像プロンプト（Claude、並列バッチ）
Phase 2b: Web 画像 URL 取得（Claude Web Search、並列）※オプション
Phase 3: 英文プロンプト → 画像（gpt-image / nanobanana、asyncio 並列）
"""

import csv
import json
import os
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from utils import get_anthropic_client, save_json, load_json, SNAPSHOT_IO_LOCK
from splitter import split_manuscript
from prompter import generate_all_prompts
from web_searcher import run_web_search, run_web_search_for_selections
from router import route_all_sentences, AI_ROUTES
from generator import (
    run_parallel_generation,
    DEFAULT_CONCURRENCY,
    PROVIDER_NANOBANANA,
    PROVIDER_GPT_IMAGE,
    VALID_PROVIDERS,
)


VALID_STYLES = ("flat_infographic", "pictogram", "comic", "whiteboard", "soviet_propaganda")
VALID_ROUTE_MODES = ("auto", "all_ai")


class SentencePipeline:
    """センテンス単位の図解生成パイプライン"""

    def __init__(
        self,
        manuscript_text: str,
        output_dir: Path,
        user_instructions: str = "",
        concurrency: int = DEFAULT_CONCURRENCY,
        provider: str = PROVIDER_NANOBANANA,
        openai_quality: str = "medium",
        style_preset: str = "flat_infographic",
        worldview_desc: str = "",
        verify_diagrams: bool = True,
        channel_id: str = "default",
        anthropic_key: str = "",
        gemini_key: str = "",
        openai_key: str = "",
        character_ref_path: str = "",
        skip_decorative: bool = False,
        web_image_count: int = 0,
        max_diagrams: int = 150,
        route_mode: str = "auto",
        chart_engine: str = "ai",          # v3: render で chart を matplotlib 描画
        allow_charts: bool = True,         # False: chart route を diagram に変換する
        map_engine: str = "ai",            # v3: render で map を GeoJSON 描画
        allow_maps: bool = True,           # False: map route を地理関係の図解に変換する
        intro_visual_boost: int = 0,       # 冒頭N文は実写/Web写真/図解を優先
        map_route_limit: int = 0,          # 0なら無制限。超過したmapはrealphotoへ寄せる
        realistic_route_min: int = 0,      # web_photo + realphoto を最低何件まで増やすか
        no_image_text: bool = False,       # True: AI図解/イラストの allowed_terms を空にする
        photo_source: str = "web",         # v3: commons で Wikimedia Commons 限定（権利安全）
        web_search_profile: str = "",      # channel別: primary_media で一次情報/動画/記事を優先
        max_web_image_reuse: int = 2,       # 同じWeb写真/サムネイルの採用上限
        type_providers: Optional[dict] = None,  # route/type別の画像生成モデル上書き
        beat_mode: bool = False,           # v3: ビート単位で重要度加重配分（False=v2均等）
        chars_per_sec: float = 5.5,        # v3: 読み上げ速度（推定タイムコード用）
        realphoto_watermark: bool = False,  # v3: realphoto に「イメージ」焼き込み
        chart_theme: Optional[dict] = None,  # v3: チャンネル別チャート/地図配色
        generation_batch_size: int = 0,       # 大量生成を章/ブロック単位で小分けにする
        generation_batch_mode: str = "block",  # block / chapter
        router_concurrency: int = 2,          # Claudeルーター分類の同時数（長文は低めが安定）
        title_override: str = "",           # v3 Step7: final.json の tentative_title
        fact_context: str = "",             # v3 Step7: final.json の検証済み数値・出典
        resume: bool = False,               # 途中で止まったジョブの再開（ディスク上の成果物を再利用）
        build_source_pack: bool = False,    # 制作資料パック（元ネタ動画+一次資料の sources.html/md）を作る
        source_videos_per_chapter: int = 3,  # 章ごとの元ネタ動画の最大収集数
        progress_callback: Optional[Callable] = None,
        log_callback: Optional[Callable] = None,
        item_callback: Optional[Callable] = None,
    ):
        self.resume = bool(resume)
        self.build_source_pack = bool(build_source_pack)
        self.source_videos_per_chapter = max(1, min(int(source_videos_per_chapter or 3), 5))
        self.manuscript_text = manuscript_text
        self.output_dir = Path(output_dir)
        self.user_instructions = user_instructions
        self.concurrency = concurrency
        self.provider = provider if provider in VALID_PROVIDERS else PROVIDER_NANOBANANA
        self.openai_quality = openai_quality
        self.style_preset = style_preset if style_preset in VALID_STYLES else "flat_infographic"
        self.worldview_desc = worldview_desc or ""
        self.verify_diagrams = bool(verify_diagrams)
        self.channel_id = channel_id or "default"
        self.anthropic_key = anthropic_key or ""
        self.gemini_key = gemini_key or ""
        self.openai_key = openai_key or ""
        self.character_ref_path = character_ref_path or ""
        self.skip_decorative = skip_decorative
        self.web_image_count = max(0, min(web_image_count, 200))
        self.max_diagrams = max(1, min(max_diagrams, 300))
        self.route_mode = route_mode if route_mode in VALID_ROUTE_MODES else "auto"
        self.chart_engine = (chart_engine or "ai").strip()  # "render" で matplotlib 描画
        self.allow_charts = bool(allow_charts)
        self.map_engine = (map_engine or "ai").strip()       # "render" で GeoJSON 描画
        self.allow_maps = bool(allow_maps)
        self.intro_visual_boost = max(0, min(int(intro_visual_boost or 0), 30))
        self.map_route_limit = max(0, min(int(map_route_limit or 0), 60))
        self.realistic_route_min = max(0, min(int(realistic_route_min or 0), 180))
        self.no_image_text = bool(no_image_text)
        self.photo_source = (photo_source or "web").strip()  # "commons" で Commons 限定
        self.web_search_profile = (web_search_profile or "").strip()
        self.max_web_image_reuse = max(1, min(int(max_web_image_reuse or 2), 10))
        self.type_providers = {
            str(k): str(v) for k, v in (type_providers or {}).items()
            if str(v) in VALID_PROVIDERS
        }
        self.beat_mode = bool(beat_mode)                     # v3: ビート加重配分
        self.chars_per_sec = float(chars_per_sec or 5.5)
        self.realphoto_watermark = bool(realphoto_watermark)  # v3: 「イメージ」焼き込み
        self.chart_theme = chart_theme or None
        self.generation_batch_size = max(0, min(int(generation_batch_size or 0), 120))
        self.generation_batch_mode = generation_batch_mode if generation_batch_mode in ("block", "chapter") else "block"
        self.router_concurrency = max(1, min(int(router_concurrency or 2), 4))
        self.title_override = (title_override or "").strip()   # v3 Step7
        self.fact_context = (fact_context or "").strip()       # v3 Step7
        self.progress_callback = progress_callback or (lambda phase, msg, pct: None)
        self.log_callback = log_callback or (lambda *a, **kw: None)
        self.item_callback = item_callback or (lambda info: None)

        self.images_dir = self.output_dir / "images"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)

        self._rows_state: dict = {}
        self._rows_lock = threading.Lock()

    def _apply_intro_visual_boost(self, rows: list, routes: dict) -> int:
        """冒頭だけ視聴維持優先で、実写/Web写真/図解に寄せる。

        図解で説明に入る前に、実写・Web写真で「現実の話」感を出すための補正。
        内容のない繋ぎも、冒頭では薄い実写背景として使えるようにする。
        """
        if self.intro_visual_boost <= 0:
            return 0

        changed = 0
        physical_words = (
            "ロシア", "ソ連", "ベラルーシ", "ウクライナ", "欧州", "EU", "NATO",
            "国境", "地図", "経由", "ルート", "進軍", "軍", "都市", "街", "施設",
            "パイプライン", "港", "鉄道", "デモ", "抗議", "会談", "大統領", "写真",
            "映像", "戦争", "侵攻", "歴史", "崩壊"
        )
        map_words = ("地図", "地理", "国境線", "位置関係", "領土", "ルート図", "地政学")
        web_words = ("大統領", "首相", "会談", "演説", "写真", "映像", "崩壊", "デモ", "抗議")

        for r in rows[:self.intro_visual_boost]:
            no = r["no"]
            rt = routes.get(no, {})
            current = rt.get("route", "")
            if current in ("web_photo", "realphoto") or (self.allow_maps and current == "map"):
                continue
            text = f"{r.get('chapter_title','')} {r.get('block_text','')} {r.get('sentence','')}"
            compact = text.replace(" ", "")
            if any(w in compact for w in map_words):
                new_route = "map" if self.allow_maps else "diagram"
            elif any(w in compact for w in web_words):
                new_route = "web_photo"
            elif current in ("skip", "diagram", "illustration", "chart") or any(w in compact for w in physical_words):
                new_route = "realphoto"
            else:
                continue
            routes[no] = {
                **rt,
                "route": new_route,
                "reason": f"冒頭{self.intro_visual_boost}文は実写/Web写真/図解優先",
                "search_query": (r.get("sentence", "") or "")[:30] if new_route == "web_photo" else "",
                "topic": (r.get("sentence", "") or "")[:18] if new_route == "web_photo" else "",
                "importance": max(3, int(rt.get("importance", 3) or 3)),
                "beat": "new",
            }
            changed += 1
        if changed:
            self._log(
                "router",
                f"冒頭実写ブースト: {changed} 件を実写/Web写真/図解へ補正",
                f"intro_visual_boost={self.intro_visual_boost}"
            )
        return changed

    def _disable_map_routes(self, rows: list, routes: dict) -> int:
        """map を使わず、位置関係を説明するシンプルな図解へ変換する。"""
        if self.allow_maps:
            return 0
        changed = 0
        row_by_no = {r["no"]: r for r in rows}
        for no, rt in routes.items():
            if rt.get("route") != "map":
                continue
            row = row_by_no.get(no, {})
            rt["route"] = "diagram"
            rt["engine"] = "ai"
            rt["reason"] = "地図なし設定: 位置関係・ルート・勢力圏をシンプル図解で表現"
            rt["visual_hint"] = (
                "Use a clean relationship/route diagram instead of a geographic map: "
                "boxes, arrows, region labels, corridors, and influence zones; no map outlines."
            )
            changed += 1
            self._update_row(
                no,
                route="diagram",
                engine="ai",
                route_reason=rt["reason"],
                visual_hint=rt["visual_hint"],
                sentence=row.get("sentence", ""),
            )
        if changed:
            self._log(
                "router",
                f"地図なし設定: map {changed} 件を位置関係図解へ変換",
                "地図のごちゃつきを避け、矢印・ラベル・領域ブロックで説明します",
            )
        return changed

    def _limit_map_routes(self, rows: list, routes: dict) -> int:
        """地図が多すぎる時は、位置関係の説明に必要なものだけ残す。"""
        if self.map_route_limit <= 0:
            return 0

        row_by_no = {r["no"]: r for r in rows}
        map_items = []
        strong_terms = ("地図", "地理", "国境線", "位置関係", "領土", "ルート図", "地政学")
        weak_terms = ("経由", "進軍", "EU", "NATO", "欧州", "ロシア", "ウクライナ", "ベラルーシ")
        for no, rt in routes.items():
            if rt.get("route") != "map":
                continue
            r = row_by_no.get(no, {})
            text = f"{r.get('chapter_title','')} {r.get('block_text','')} {r.get('sentence','')}"
            compact = text.replace(" ", "")
            score = int(rt.get("importance", 3) or 3)
            score += sum(3 for w in strong_terms if w in compact)
            score += sum(1 for w in weak_terms if w in compact)
            map_items.append((score, no))

        if len(map_items) <= self.map_route_limit:
            return 0

        keep = {no for _, no in sorted(map_items, reverse=True)[:self.map_route_limit]}
        changed = 0
        for _, no in map_items:
            if no in keep:
                continue
            rt = routes[no]
            rt["route"] = "realphoto"
            rt["reason"] = "地図枚数上限により実写風へ変換"
            rt["engine"] = "ai"
            changed += 1
        self._log(
            "router",
            f"地図比率調整: map {changed} 件を realphoto に変換",
            f"map_route_limit={self.map_route_limit}"
        )
        return changed

    def _boost_realistic_routes(self, rows: list, routes: dict) -> int:
        """実写・Web写真比率を増やす。既存の地図/Web/実写は尊重する。"""
        if self.realistic_route_min <= 0:
            return 0

        current = sum(1 for rt in routes.values() if rt.get("route") in ("web_photo", "realphoto"))
        need = self.realistic_route_min - current
        if need <= 0:
            return 0

        web_words = (
            "大統領", "首相", "会談", "演説", "写真", "映像", "デモ", "抗議", "崩壊",
            "選挙", "軍事施設", "歴史写真", "ニュース", "発表", "記者会見", "条約"
        )
        real_words = (
            "ロシア", "ソ連", "ベラルーシ", "ウクライナ", "欧州", "EU", "NATO",
            "軍", "都市", "街", "施設", "パイプライン", "港", "鉄道", "戦争",
            "侵攻", "歴史", "経済", "エネルギー", "国境"
        )
        candidates = []
        for r in rows:
            no = r["no"]
            rt = routes.get(no, {})
            route = rt.get("route", "")
            if route in ("web_photo", "realphoto", "map"):
                continue
            if route not in ("diagram", "illustration", "chart", "skip", ""):
                continue
            text = f"{r.get('chapter_title','')} {r.get('block_text','')} {r.get('sentence','')}"
            compact = text.replace(" ", "")
            web_score = sum(3 for w in web_words if w in compact)
            real_score = sum(1 for w in real_words if w in compact)
            score = web_score + real_score + int(rt.get("importance", 3) or 3)
            if score <= 2:
                continue
            target = "web_photo" if web_score >= 3 else "realphoto"
            candidates.append((score, no, target))

        changed = 0
        for _, no, target in sorted(candidates, reverse=True)[:need]:
            r = next((row for row in rows if row["no"] == no), {})
            rt = routes.get(no, {})
            routes[no] = {
                **rt,
                "route": target,
                "reason": f"実写/Web写真比率を増やす設定（最低{self.realistic_route_min}件）",
                "search_query": (r.get("sentence", "") or "")[:30] if target == "web_photo" else rt.get("search_query", ""),
                "topic": (r.get("sentence", "") or "")[:18] if target == "web_photo" else rt.get("topic", ""),
                "importance": max(3, int(rt.get("importance", 3) or 3)),
                "beat": "new",
            }
            changed += 1
        if changed:
            self._log(
                "router",
                f"実写/Web写真ブースト: {changed} 件を web_photo/realphoto へ補正",
                f"realistic_route_min={self.realistic_route_min}"
            )
        return changed

    def _remove_image_text_terms(self, rows_with_prompts: list) -> int:
        """文字なし運用のチャンネルでは、画像内テキスト許可語を全部消す。"""
        if not self.no_image_text:
            return 0
        changed = 0
        for r in rows_with_prompts:
            if r.get("allowed_terms"):
                r["allowed_terms"] = []
                changed += 1
        if changed:
            self._log("prompter", f"文字なし設定: allowed_terms {changed} 件を空にしました")
        return changed

    def _attach_diagram_context(self, prompt_rows: list, all_rows: list, window: int = 2) -> list:
        """図解設計だけに使う前後文脈を付与する。画像内ラベルは sentence 側で制限する。"""
        if not prompt_rows or not all_rows:
            return prompt_rows
        ordered = sorted(all_rows, key=lambda r: int(r.get("no", 0) or 0))
        index_by_no = {int(r.get("no", 0) or 0): i for i, r in enumerate(ordered)}
        out = []
        changed = 0
        for r in prompt_rows:
            rr = dict(r)
            route = rr.get("route") or rr.get("type")
            no = int(rr.get("no", 0) or 0)
            if route == "diagram" and no in index_by_no:
                i = index_by_no[no]
                start = max(0, i - window)
                end = min(len(ordered), i + window + 1)
                lines = []
                for ctx in ordered[start:end]:
                    marker = "対象" if int(ctx.get("no", 0) or 0) == no else "前後"
                    sent = (ctx.get("sentence") or "").strip()
                    if sent:
                        lines.append(f"{marker}#{ctx.get('no')}: {sent}")
                if lines:
                    rr["diagram_context"] = "\n".join(lines)
                    changed += 1
            out.append(rr)
        if changed:
            self._log("prompter", f"図解設計用の前後文脈を付与: {changed} 件", "前後2文を構造理解に使用")
        return out

    # ---- ヘルパ ----
    def _log(self, category: str, message: str, detail: str = ""):
        print(f"  [{category}] {message}" + (f" - {detail}" if detail else ""), flush=True)
        try:
            self.log_callback(category, message, detail)
        except Exception:
            pass

    def _progress(self, phase: int, message: str, percent: int):
        print(f"  [Phase {phase}] {message} ({percent}%)", flush=True)
        try:
            self.progress_callback(phase, message, percent)
        except Exception:
            pass

    def _update_row(self, no: int, **fields):
        with self._rows_lock:
            r = self._rows_state.get(no, {})
            r.update(fields)
            self._rows_state[no] = r
        self._dump_snapshot()
        try:
            self.item_callback({"no": no, **fields})
        except Exception:
            pass

    def _dump_snapshot(self):
        with self._rows_lock:
            rows = sorted(self._rows_state.values(), key=lambda x: x.get("no", 0))
        snapshot = {
            "rows": rows,
            "updated_at": datetime.now().isoformat(),
        }
        # 再生成エンドポイント（_update_regen_snapshot）と同じファイルを書くため、
        # 共有ロックで書き込みを直列化（同時書き込みによる更新消失を防ぐ）
        with SNAPSHOT_IO_LOCK:
            try:
                (self.output_dir / "rows_progress.json").write_text(
                    json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
                )
            except Exception:
                pass

    # ---- 画像配置ロジック ----
    def _wants_image(self, r) -> bool:
        """この文を今回のジョブで画像化するか。

        beat_mode=True のとき allocator が選んだ display=image の文のみ。
        False は v2（全候補→均等間引きに委ねる）。
        """
        if self.beat_mode:
            return r.get("display") == "image"
        return True

    def _existing_image(self, no) -> str:
        """再開用: その文の生成済み画像がディスクにあればファイル名を返す。

        前回の実行で保存された images/{no}.png 等（1KB超＝壊れていない）を
        完成済みとみなし、再生成をスキップする根拠にする。
        """
        for ext in ("png", "jpg", "jpeg", "webp"):
            p = self.images_dir / f"{no}.{ext}"
            try:
                if p.exists() and p.stat().st_size > 1024:
                    return p.name
            except OSError:
                continue
        return ""

    def _force_high_coverage_images(self, rows: list, routes: dict) -> int:
        """大量生成指定では、beat/skip による減りすぎを補正する。

        通常の50枚/150枚生成では「重要なビートだけ画像化」が自然だが、
        250〜300枚指定ではユーザー期待は「ほぼ全文に画像を付ける」こと。
        そのため display=hold/none や route=skip も、上限に届くまで画像対象へ戻す。
        """
        if not self.beat_mode or not rows:
            return 0

        total = len(rows)
        high_coverage_requested = self.max_diagrams >= 250 or self.max_diagrams >= int(total * 0.85)
        if not high_coverage_requested:
            return 0

        target = min(self.max_diagrams, total)
        current = [r for r in rows if r.get("display") == "image"]
        missing = target - len(current)
        if missing <= 0:
            return 0

        candidates = [r for r in rows if r.get("display") != "image"]
        chosen_nos = self._select_evenly_distributed(candidates, missing)
        changed = 0
        for r in rows:
            no = r["no"]
            if no not in chosen_nos:
                continue
            r["display"] = "image"
            rt = routes.setdefault(no, {})
            if rt.get("route") == "skip":
                rt["route"] = "illustration"
                rt["reason"] = "300枚級の大量生成指定のため、skipを画像化対象へ補正"
                r["route"] = "illustration"
                r["route_reason"] = rt["reason"]
            self._update_row(
                no,
                display="image",
                route=r.get("route", rt.get("route", "illustration")),
                route_reason=r.get("route_reason", rt.get("reason", "")),
                status="pending",
            )
            changed += 1

        if changed:
            self._log(
                "allocator",
                f"大量生成補正: {changed} 文を画像対象へ追加（目標 {target} 枚）",
                "300枚指定時に skip/hold で画像数が減りすぎる問題を防ぎます",
            )
        return changed

    @staticmethod
    def _select_evenly_distributed(candidates: list, max_count: int) -> set:
        """候補センテンスから max_count 個を全文均等に間引いて選定する。

        - 候補数 <= max_count: 全部選ぶ
        - 候補数 > max_count: 順序を保ったまま等間隔でサンプリング
          例: 候補 250, max 50 → 5 ステップごとに 1 つ選ぶ
              実装は浮動小数演算で「最も均等な分布」を実現

        戻り値: 選定された row["no"] の set
        """
        n_cand = len(candidates)
        if n_cand == 0 or max_count <= 0:
            return set()
        if n_cand <= max_count:
            return {r["no"] for r in candidates}

        # 等間隔サンプリング: index i (0..max-1) → round((i + 0.5) * n / max)
        # (i + 0.5) を使うことで「先頭・末尾に寄らず中央付近にも均等配置」される
        step = n_cand / max_count
        selected: set = set()
        for i in range(max_count):
            idx = int((i + 0.5) * step)
            if idx >= n_cand:
                idx = n_cand - 1
            selected.add(candidates[idx]["no"])

        # 万一重複でズレた分を補充（小さい数なので O(n) で十分）
        if len(selected) < max_count:
            for r in candidates:
                if r["no"] not in selected:
                    selected.add(r["no"])
                    if len(selected) >= max_count:
                        break

        return selected

    @classmethod
    def _chunk_generation_targets(cls, targets: list, batch_size: int, mode: str = "block") -> list:
        """章/ブロック境界をなるべく保ちながら画像生成対象を小分けする。"""
        if not targets:
            return []
        if mode == "chapter":
            chapter_chunks = []
            current = []
            current_chapter = targets[0].get("chapter_index")
            for t in targets:
                chapter = t.get("chapter_index")
                if current and chapter != current_chapter:
                    chapter_chunks.extend(cls._chunk_generation_targets(current, batch_size, mode="block"))
                    current = []
                current.append(t)
                current_chapter = chapter
            if current:
                chapter_chunks.extend(cls._chunk_generation_targets(current, batch_size, mode="block"))
            return chapter_chunks

        if batch_size <= 0 or len(targets) <= batch_size:
            return [targets]

        chunks = []
        current = []
        current_chapter = None
        current_block = None

        for t in targets:
            chapter = t.get("chapter_index")
            block = t.get("block_index")
            boundary_changed = (
                current
                and (chapter != current_chapter or block != current_block)
            )
            if current and len(current) >= batch_size and boundary_changed:
                chunks.append(current)
                current = []

            current.append(t)
            current_chapter = chapter
            current_block = block

            # 1つのブロック/章が大きすぎる場合でも、batch_size の約1.5倍で必ず切る。
            if len(current) >= int(batch_size * 1.5):
                chunks.append(current)
                current = []
                current_chapter = None
                current_block = None

        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def _web_image_dedupe_key(info: dict) -> str:
        """Web写真の重複判定キー。サムネがあれば画像単位、無ければ出典単位で見る。"""
        key = (
            info.get("thumb_url")
            or info.get("source_url")
            or info.get("commons_page_url")
            or ""
        )
        return key.strip().lower()

    def _provider_for_target(self, target: dict) -> str:
        """画像タイプ別 provider。未指定ならジョブ全体の provider を使う。"""
        return self.type_providers.get(target.get("type", ""), self.provider)

    def _save_generation_checkpoint(self, batch_idx: int, total_batches: int, batch_targets: list, batch_results: list):
        """章/ブロック単位の生成完了をディスクへ保存する。途中停止時の確認材料にする。"""
        with self._rows_lock:
            rows_snapshot = sorted(self._rows_state.values(), key=lambda x: x.get("no", 0))
        chapter_title = batch_targets[0].get("section", "") if batch_targets else ""
        payload = {
            "batch_index": batch_idx,
            "total_batches": total_batches,
            "chapter_title": chapter_title,
            "target_nos": [t.get("index") for t in batch_targets],
            "success": sum(1 for r in batch_results if r.get("success")),
            "failed": sum(1 for r in batch_results if not r.get("success")),
            "results": batch_results,
            "rows": rows_snapshot,
            "saved_at": datetime.now().isoformat(),
        }
        checkpoint_dir = self.output_dir / "checkpoints"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        save_json(checkpoint_dir / f"generation_batch_{batch_idx:03d}.json", payload)
        save_json(self.output_dir / "latest_generation_checkpoint.json", payload)

    @staticmethod
    def _provider_label(provider: str, openai_quality: str = "medium") -> str:
        if provider == PROVIDER_NANOBANANA:
            return "nanobanana (Gemini)"
        if provider == PROVIDER_GPT_IMAGE:
            return f"gpt-image ({openai_quality})"
        return provider

    def _load_route_feedback(self, limit: int = 12) -> list:
        """v3 Step5: 過去の「ルート違い」フィードバックを読み、ルーターに渡す few-shot を作る。

        route_feedback.jsonl（output ルート直下＝全ジョブ共通）から、同じチャンネルの
        記録だけを新しい順に最大 limit 件返す。ファイルが無い／壊れていても落とさない。
        """
        fb_path = self.output_dir.parent / "route_feedback.jsonl"
        if not fb_path.exists():
            return []
        records = []
        try:
            with open(fb_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("channel_id", "default") != self.channel_id:
                        continue
                    if rec.get("sentence") and rec.get("correct_route"):
                        records.append(rec)
        except Exception:
            return []
        # 新しい順（末尾優先）に limit 件
        return records[-limit:]

    # ---- メインフロー ----
    def run(self) -> dict:
        # チャンネル別キーがあれば優先、無ければ共通（環境変数）
        client = get_anthropic_client(self.anthropic_key)
        gemini_key = (self.gemini_key or "").strip() or os.environ.get("GEMINI_API_KEY", "")
        openai_key = (self.openai_key or "").strip() or os.environ.get("OPENAI_API_KEY", "")

        if self.provider == PROVIDER_NANOBANANA and not gemini_key:
            raise RuntimeError("nanobanana を使うには GEMINI_API_KEY が必要です。")
        if self.provider == PROVIDER_GPT_IMAGE and not openai_key:
            raise RuntimeError("gpt-image を使うには OPENAI_API_KEY が必要です。")
        if PROVIDER_NANOBANANA in self.type_providers.values() and not gemini_key:
            raise RuntimeError("タイプ別生成で nanobanana を使うには GEMINI_API_KEY が必要です。")
        if PROVIDER_GPT_IMAGE in self.type_providers.values() and not openai_key:
            raise RuntimeError("タイプ別生成で gpt-image を使うには OPENAI_API_KEY が必要です。")

        # Phase 0
        self._progress(0, "原稿を保存中...", 1)
        manuscript_path = self.output_dir / "manuscript.txt"
        manuscript_path.write_text(self.manuscript_text, encoding="utf-8")
        self._log("setup", f"原稿を保存しました（{len(self.manuscript_text)}文字）")

        # ===== Phase 1: 分解 =====
        self._progress(1, "原稿を章/ブロック/センテンスに分解中...", 5)
        self._log("splitter", "Claude で原稿を分解しています...")
        # .docx の見出しから作った章構造があれば使う
        prebuilt = None
        pc_path = self.output_dir / "prebuilt_chapters.json"
        if pc_path.exists():
            try:
                prebuilt = load_json(pc_path, {}).get("chapters") or None
            except Exception:
                prebuilt = None
        split_result = None
        if self.resume:
            cached_split = load_json(self.output_dir / "split_result.json", None)
            if isinstance(cached_split, dict) and cached_split.get("rows"):
                split_result = cached_split
                self._log("resume",
                          f"再開: 前回の分解結果を再利用（{len(split_result['rows'])} 文・Claude呼び出しスキップ）")
        if split_result is None:
            split_result = split_manuscript(client, self.manuscript_text, log=self._log,
                                            prebuilt_chapters=prebuilt)
        analysis = split_result["analysis"]
        chapters = split_result["chapters"]
        rows = split_result["rows"]
        total_sentences = split_result["total_sentences"]
        # v3 Step7: final.json の tentative_title があれば最優先（無ければ分解で推定）
        title = self.title_override or analysis.get("title", "無題")

        # 上限を超える場合は、v2 は全文均等配置、v3(beat_mode) は重要度配分で間引く。
        if total_sentences > self.max_diagrams:
            self._log("warn",
                      f"センテンス {total_sentences} 個が上限 {self.max_diagrams} を超過。全文から均等/重要度配分で {self.max_diagrams} 件を画像化します。",
                      "先頭だけで打ち切らず、全文に画像が散るように配置します")

        self._log("splitter", f"分解完了: {title}", f"章 {len(chapters)} / センテンス {total_sentences}")
        save_json(self.output_dir / "split_result.json", split_result)

        with self._rows_lock:
            for r in rows:
                self._rows_state[r["no"]] = {
                    **r,
                    "status": "pending",
                    "filename": None,
                    "prompt": "",
                    "allowed_terms": [],
                    "type": "",
                    "route": "",
                    "route_reason": "",
                    "web_source_url": "",
                    "web_thumb_url": "",
                    "web_topic": "",
                }
        self._dump_snapshot()
        self._progress(1, f"分解完了: {total_sentences} センテンス検出", 15)

        # ===== Phase 2-router: 各文のソースを判定 =====
        # 再開時: 前回保存した routes.json（boost 等の後処理適用済み）を再利用する。
        # JSON 経由でキーが文字列化されているため int に正規化。全文をカバーして
        # いなければ（途中保存など）安全側に倒して作り直す。
        routes = None
        if self.resume:
            cached_routes = load_json(self.output_dir / "routes.json", None)
            if isinstance(cached_routes, dict) and cached_routes:
                try:
                    normalized = {int(k): v for k, v in cached_routes.items()}
                except (TypeError, ValueError):
                    normalized = None
                if normalized and all(r["no"] in normalized for r in rows):
                    routes = normalized
                    self._log("resume",
                              f"再開: ルート判定を再利用（{len(routes)} 件・Claude呼び出しスキップ）")
        if routes is None:
            if self.route_mode == "auto":
                self._progress(2, "各文のソースを判定中（ルーター）...", 16)
                self._log("router", "ルーターが各文の最適なソースを判定します")
                few_shot = self._load_route_feedback()
                if few_shot:
                    self._log("router", f"過去のルート違いフィードバック {len(few_shot)} 件を学習に反映します")
                routes = route_all_sentences(
                    client, rows, title,
                    user_instructions=self.user_instructions,
                    max_workers=self.router_concurrency, log=self._log,
                    few_shot=few_shot,
                )
            else:  # all_ai: v1 互換（全文 AI 生成）
                self._log("router", "route_mode=all_ai: 全文を AI 生成に回します")
                routes = {
                    r["no"]: {"route": "illustration", "reason": "all_ai モード", "search_query": "", "topic": "", "propaganda": False}
                    for r in rows
                }
            # route の後処理（boost 等）は新規計算時のみ。routes.json は処理済みを
            # 保存しているので、再利用時に再適用すると二重適用になる。
            self._apply_intro_visual_boost(rows, routes)
            self._disable_map_routes(rows, routes)
            self._limit_map_routes(rows, routes)
            self._boost_realistic_routes(rows, routes)

            if not self.allow_charts:
                converted = 0
                for rt in routes.values():
                    if rt.get("route") == "chart":
                        rt["route"] = "diagram"
                        rt["reason"] = "チャンネル設定でグラフなし"
                        converted += 1
                if converted:
                    self._log("router", f"グラフなし設定: chart {converted} 件を diagram に変換")

        save_json(self.output_dir / "routes.json", routes)

        for r in rows:
            rt = routes.get(r["no"], {})
            r["route"] = rt.get("route", "illustration")
            r["route_reason"] = rt.get("reason", "")
        for no, rt in routes.items():
            self._update_row(no, route=rt.get("route", "illustration"), route_reason=rt.get("reason", ""))

        # ===== v3 Step4: ビート確定・タイムコード・重要度加重配分（LLM不使用）=====
        # beat_mode=True のとき max_diagrams をビート単位で重要度加重配分し、画像を付ける
        # 文(display=image)だけを画像化する。False は v2（均等間引き）。失敗時は v2 に倒す。
        try:
            from allocator import allocate, write_allocation
            alloc = allocate(rows, routes, self.max_diagrams,
                             chars_per_sec=self.chars_per_sec, beat_mode=self.beat_mode)
            for r in rows:
                a = alloc.get(r["no"], {})
                r["beat_id"] = a.get("beat_id")
                r["est_start"] = a.get("est_start", "")
                r["display"] = a.get("display", "none")
                r["importance"] = a.get("importance", 3)
                self._update_row(r["no"], beat_id=r["beat_id"],
                                 est_start=r["est_start"], importance=r["importance"],
                                 display=r["display"])
            write_allocation(self.output_dir / "allocation.json", rows, routes, alloc)
            if self.beat_mode:
                n_img = sum(1 for r in rows if r.get("display") == "image")
                n_hold = sum(1 for r in rows if r.get("display") == "hold")
                self._log("allocator",
                          f"ビート配分: 画像 {n_img} 枚 / 継続(hold) {n_hold} 文"
                          f"（重要度加重・上限 {self.max_diagrams}）")
                for r in rows:
                    if r.get("display") == "hold":
                        self._update_row(r["no"], status="hold")
                self._force_high_coverage_images(rows, routes)
        except Exception as e:
            self._log("error", f"allocator をスキップ（{str(e)[:80]}）。v2 配分にフォールバック。")
            self.beat_mode = False

        # ===== v3 Step1: chart を matplotlib で決定論レンダリング（engine:render）=====
        # chart_engine=render のとき chart 文の数値を抽出し正確に描画。抽出不能/描画失敗は
        # diagram(engine:ai) へ降格して v2 同様 AI 生成へ。何が起きても v2 にフォールバック。
        for r in rows:
            r.setdefault("engine", "ai")
        if self.chart_engine == "render":
            try:
                from router import extract_chart_specs
                chart_rows = [r for r in rows if r.get("route") == "chart" and self._wants_image(r)]
                # 再開時: 前回描画済みのグラフはスキップ（抽出・描画とも不要）
                if self.resume and chart_rows:
                    remaining = []
                    for r in chart_rows:
                        fn = self._existing_image(r["no"])
                        if fn:
                            r["engine"] = "render"
                            self._update_row(r["no"], status="ok", filename=fn, engine="render")
                        else:
                            remaining.append(r)
                    if len(remaining) < len(chart_rows):
                        self._log("resume", f"再開: 描画済みグラフ {len(chart_rows) - len(remaining)} 枚を再利用")
                    chart_rows = remaining
                if chart_rows:
                    self._progress(2, "chart の数値を抽出して図を描画中...", 20)
                    specs = extract_chart_specs(client, chart_rows, log=self._log,
                                                extra_context=self.fact_context)
                    to_render = []
                    for r in chart_rows:
                        spec = specs.get(r["no"])
                        if spec:
                            r["engine"] = "render"
                            r["chart_spec"] = spec
                            to_render.append(r)
                        else:
                            r["route"] = "diagram"  # 降格（chart→diagram, engine:ai）
                            r["engine"] = "ai"
                            self._update_row(r["no"], route="diagram")
                    if to_render:
                        self._render_charts(to_render)
            except Exception as e:
                self._log("error", f"chartレンダリングをスキップ（{str(e)[:80]}）。AI生成に回します。")
                for r in rows:
                    if r.get("route") == "chart" and r.get("engine") == "render":
                        r["engine"] = "ai"
        # ----- map（Step2）: route=map を Natural Earth GeoJSON で正確描画。降格先は illustration -----
        if self.allow_maps and self.map_engine == "render":
            try:
                from router import extract_map_specs
                map_rows = [r for r in rows if r.get("route") == "map" and self._wants_image(r)]
                # 再開時: 前回描画済みの地図はスキップ
                if self.resume and map_rows:
                    remaining = []
                    for r in map_rows:
                        fn = self._existing_image(r["no"])
                        if fn:
                            r["engine"] = "render"
                            self._update_row(r["no"], status="ok", filename=fn, engine="render")
                        else:
                            remaining.append(r)
                    if len(remaining) < len(map_rows):
                        self._log("resume", f"再開: 描画済み地図 {len(map_rows) - len(remaining)} 枚を再利用")
                    map_rows = remaining
                if map_rows:
                    self._progress(2, "地図データを抽出して描画中...", 21)
                    specs = extract_map_specs(client, map_rows, log=self._log)
                    to_render = []
                    for r in map_rows:
                        spec = specs.get(r["no"])
                        if spec:
                            r["engine"] = "render"
                            r["map_spec"] = spec
                            to_render.append(r)
                        else:
                            r["route"] = "illustration"  # 降格（map→illustration, engine:ai）
                            r["engine"] = "ai"
                            self._update_row(r["no"], route="illustration")
                    if to_render:
                        self._render_maps(to_render)
            except Exception as e:
                self._log("error", f"地図レンダリングをスキップ（{str(e)[:80]}）。AI生成に回します。")
                for r in rows:
                    if r.get("route") == "map" and r.get("engine") == "render":
                        r["engine"] = "ai"
        for r in rows:
            self._update_row(r["no"], engine=r.get("engine", "ai"))

        # メモリ解放: 地図用 GeoJSON（shapely 幾何・数十MB）を生成フェーズ前に手放す。
        # 画像生成が最もメモリを使うため、ここで返すと 512MB 環境の OOM を緩和できる。
        try:
            import gc
            from renderer import clear_geo_cache
            clear_geo_cache()
            gc.collect()
        except Exception:
            pass

        # route で分類（engine:render はレンダリング済みなので AI 対象から除外）
        web_photo_rows = [r for r in rows if r.get("route") == "web_photo"]
        ai_rows = [r for r in rows if r.get("route") in AI_ROUTES
                   and r.get("engine") != "render" and self._wants_image(r)]
        skip_rows = [r for r in rows if r.get("route") == "skip"]

        # skip 文をマーク
        for r in skip_rows:
            self._update_row(r["no"], status="skipped")

        rendered_rows = [r for r in rows if r.get("engine") == "render" and self._wants_image(r)]
        hold_rows = [r for r in rows if r.get("display") == "hold"]
        no_image_rows = [r for r in rows if r.get("display") == "none"]
        self._log(
            "router",
            f"振り分け: AI生成 {len(ai_rows)} / render済み {len(rendered_rows)} / Web写真 {len(web_photo_rows)} / skip {len(skip_rows)}",
            f"hold {len(hold_rows)} / none {len(no_image_rows)} / beat_mode={self.beat_mode}"
        )

        # ===== Phase 2a: 英文プロンプト（AI 行のみ） =====
        self._progress(2, f"英文プロンプトを並列生成中（style={self.style_preset}）...", 22)
        self._log("prompter", f"{len(ai_rows)} 件（AI生成対象）のプロンプトを生成します")
        ai_rows_for_prompts = self._attach_diagram_context(ai_rows, rows)
        # 再開時: 前回のプロンプトが今回の対象を全てカバーしていれば再利用（Claude節約）。
        # 対象が変わっていたら（ルート再計算等）安全側に倒して作り直す。
        rows_with_prompts = None
        if self.resume:
            cached_prompts = (load_json(self.output_dir / "prompts.json", {}) or {}).get("rows")
            if cached_prompts:
                need = {r["no"] for r in ai_rows_for_prompts}
                have = {r.get("no") for r in cached_prompts if r.get("prompt")}
                if need and need.issubset(have):
                    rows_with_prompts = cached_prompts
                    self._log("resume",
                              f"再開: 画像プロンプトを再利用（{len(rows_with_prompts)} 件・Claude呼び出しスキップ）")
        if rows_with_prompts is None:
            rows_with_prompts = generate_all_prompts(
                client, ai_rows_for_prompts, title=title,
                user_instructions=self.user_instructions,
                style_preset=self.style_preset, worldview_desc=self.worldview_desc,
                max_workers=6, log=self._log,
            )
            self._remove_image_text_terms(rows_with_prompts)
        save_json(self.output_dir / "prompts.json", {"rows": rows_with_prompts})
        self._log("prompter", f"プロンプト生成完了: {len(rows_with_prompts)} 件")

        for r in rows_with_prompts:
            self._update_row(
                r["no"],
                prompt=r.get("prompt", ""),
                allowed_terms=r.get("allowed_terms", []),
                diagram_blueprint=r.get("diagram_blueprint", {}),
                type=r.get("type", "illustration"),
            )
        self._progress(2, "プロンプト生成完了", 35)

        # ===== Phase 2b: Web 画像 URL 取得（並列実行） =====
        # 部分結果を保持する list（タイムアウト時にも参照できる）
        web_results_accumulator: list = []
        web_image_use_counts: dict = {}
        web_image_use_lock = threading.Lock()

        def _web_on_item(info):
            dedupe_key = self._web_image_dedupe_key(info)
            if dedupe_key:
                with web_image_use_lock:
                    used = web_image_use_counts.get(dedupe_key, 0)
                    if used >= self.max_web_image_reuse:
                        self._log(
                            "websearch",
                            f"重複Web写真をスキップ: no={info.get('no')} / 既に {used} 回使用",
                            (info.get("source_title") or info.get("source_url") or "")[:120],
                        )
                        self._update_row(
                            info["no"],
                            web_source_url=info.get("source_url", ""),
                            web_thumb_url=info.get("thumb_url", ""),
                            web_topic=info.get("topic", ""),
                            web_source_title=info.get("source_title", ""),
                            web_source_type=info.get("source_type", ""),
                            web_duplicate_skipped=True,
                            error=f"同じWeb写真が上限{self.max_web_image_reuse}回に達したためAI代替へ回します",
                        )
                        return
                    web_image_use_counts[dedupe_key] = used + 1

            web_results_accumulator.append(info)
            # サムネをローカルに DL して実画像として表示・ZIP 同梱できるようにする
            from web_searcher import download_thumbnail
            local_file = ""
            thumb_url = info.get("thumb_url", "")
            if thumb_url:
                fname = f"{info['no']}.jpg"  # 数字だけのファイル名（№と一致）
                existing = self._existing_image(info["no"])  # 再開時は前回DL済みを再利用
                if existing:
                    local_file = existing
                elif download_thumbnail(thumb_url, self.images_dir / fname):
                    local_file = fname
            update = dict(
                web_source_url=info.get("source_url", ""),
                web_thumb_url=info.get("thumb_url", ""),
                web_local_file=local_file,
                web_topic=info.get("topic", ""),
                web_source_title=info.get("source_title", ""),
                web_source_type=info.get("source_type", ""),
                # v3 Step3: Commons のライセンス・クレジット（CSV / credits.txt 用）
                license=info.get("license", ""),
                attribution=info.get("attribution", ""),
                commons_page_url=info.get("commons_page_url", ""),
                web_material_type=info.get("material_type", ""),  # 資料パックの分類用
                # 差し替え候補（検索でヒットした他ページ）。UIの「候補から選ぶ」で使う
                web_candidates=[
                    {"url": u.get("url", ""), "title": u.get("title", "")}
                    for u in (info.get("all_urls") or [])[:4] if u.get("url")
                ],
            )
            if local_file:
                # Web写真も「画像1枚」として扱う。これを入れないと画面上は画像が見えても
                # 件数カウントでは pending のままになり、「50枚中8枚」のように見える。
                update["status"] = "ok"
                update["filename"] = local_file
            self._update_row(info["no"], **update)
            try:
                save_json(
                    self.output_dir / "web_results.json",
                    {"items": list(web_results_accumulator)},
                )
            except Exception:
                pass

        def _web_save_final():
            try:
                save_json(
                    self.output_dir / "web_results.json",
                    {"items": list(web_results_accumulator)},
                )
                # v3 Step3: 採用した Commons 画像のクレジット一覧（概要欄用）
                from commons_searcher import build_credits_text
                txt = build_credits_text(list(web_results_accumulator))
                (self.output_dir / "credits.txt").write_text(txt, encoding="utf-8")
            except Exception:
                pass

        web_thread = None

        # 再開時: 前回の Web/Commons 取得結果を再生してから、未取得分だけ検索する
        resumed_web_nos = set()
        if self.resume:
            cached_web = (load_json(self.output_dir / "web_results.json", {}) or {}).get("items") or []
            for info in cached_web:
                no = info.get("no")
                if no is None or no in resumed_web_nos:
                    continue
                resumed_web_nos.add(no)
                try:
                    _web_on_item(info)
                except Exception:
                    continue
            if resumed_web_nos:
                self._log("resume", f"再開: Web/Commons 画像 {len(resumed_web_nos)} 件を再利用（検索スキップ）")
                _web_save_final()

        if self.route_mode == "auto" and (web_photo_rows or self.web_search_profile == "primary_media"):
            # 通常はルーターが web_photo に振った文だけ検索。
            # primary_media（成功の法則）は、記事・一次資料・講演ページも拾うため、
            # web_photo 以外の重要文も追加選定して最大 web_image_count 件まで検索する。
            selections = []
            if self.web_search_profile == "primary_media":
                material_rows = [
                    r for r in rows
                    if r.get("route") != "skip" and r.get("display") in ("image", "hold", None, "")
                ]
                target_count = min(max(self.web_image_count, len(web_photo_rows)), 120)
                self._log(
                    "websearch",
                    f"一次情報/記事/講演ページの素材検索を拡張: 目標 {target_count} 件（候補 {len(material_rows)}）"
                )
                from web_searcher import select_search_worthy_sentences
                selections = select_search_worthy_sentences(
                    client, material_rows, target_count=target_count,
                    log=self._log, profile=self.web_search_profile,
                )
            else:
                for r in web_photo_rows:
                    if not self._wants_image(r):
                        continue
                    rt = routes.get(r["no"], {})
                    selections.append({
                        "no": r["no"],
                        "query": rt.get("search_query") or r.get("sentence", "")[:30],
                        "topic": rt.get("topic") or r.get("sentence", "")[:20],
                    })
            # 再開時: 前回取得済みの文は検索対象から外す
            if resumed_web_nos:
                selections = [s for s in selections if s.get("no") not in resumed_web_nos]
            web_workers = 8 if self.web_search_profile == "primary_media" else 4
            self._log("websearch",
                      f"Web 画像取得を並列起動: {len(selections)} 件（ルーター選定・同時 {web_workers} 並列）")

            def web_task_auto():
                try:
                    if self.photo_source == "commons":
                        # v3 Step3: Wikimedia Commons 限定（許可ライセンスのみ・権利安全）
                        from commons_searcher import run_commons_search_for_selections
                        run_commons_search_for_selections(
                            client, selections, max_workers=web_workers,
                            log=self._log, item_callback=_web_on_item,
                        )
                    else:
                        run_web_search_for_selections(
                            client, selections, max_workers=web_workers,
                            log=self._log, item_callback=_web_on_item,
                            profile=self.web_search_profile,
                        )
                except Exception as e:
                    self._log("error", f"Web/Commons 画像取得失敗: {str(e)[:120]}")
                _web_save_final()

            web_thread = threading.Thread(target=web_task_auto, daemon=True)

        elif self.route_mode == "all_ai" and self.web_image_count > 0:
            # v1 互換: web_image_count で内部選定
            self._log("websearch",
                      f"Web 画像取得を並列起動: 目標 {self.web_image_count} 件（v1 選定・同時 8 並列）")

            def web_task_v1():
                try:
                    run_web_search(
                        client, rows_with_prompts,
                        target_count=self.web_image_count, max_workers=8,
                        log=self._log, item_callback=_web_on_item,
                        profile=self.web_search_profile,
                    )
                except Exception as e:
                    self._log("error", f"Web 画像取得失敗: {str(e)[:120]}")
                _web_save_final()

            web_thread = threading.Thread(target=web_task_v1, daemon=True)

        if web_thread:
            web_thread.start()

        # ===== 制作資料パック: 元ネタ動画の収集（画像生成と並行・ベストエフォート） =====
        # 章ごとに「内容の元ネタになり得る実在の動画」（講演/インタビュー/公式ch等）を
        # Web検索で収集し、完成時に sources.html/md へまとめる（裏取り・演出参考用。動画内素材には使わない）。
        source_videos: dict = {}
        source_videos_done = threading.Event()
        if self.build_source_pack:
            cached_videos = load_json(self.output_dir / "source_videos.json", None) if self.resume else None
            if isinstance(cached_videos, dict) and cached_videos:
                source_videos.update({int(k): v for k, v in cached_videos.items() if str(k).isdigit()})
                source_videos_done.set()
                self._log("resume", f"再開: 元ネタ動画 {sum(len(v) for v in source_videos.values())} 本を再利用")
            else:
                def _collect_videos():
                    try:
                        from source_collector import collect_source_videos
                        got = collect_source_videos(
                            client, title, chapters, rows,
                            per_chapter=self.source_videos_per_chapter, log=self._log,
                            routes=routes)
                        source_videos.update(got)
                        save_json(self.output_dir / "source_videos.json", source_videos)
                    except Exception as e:
                        self._log("sources", f"元ネタ動画の収集をスキップ（{str(e)[:80]}）")
                    finally:
                        source_videos_done.set()
                self._log("sources", "元ネタ動画（講演/インタビュー/公式ch等）を並行収集します")
                threading.Thread(target=_collect_videos, daemon=True).start()
        else:
            source_videos_done.set()

        # ===== Phase 3: 画像生成（全文均等配置で選定） =====
        # Step A: skip_decorative なら decorative 行を先に除外（候補から外す）
        candidates = []
        skipped_decorative = 0
        for r in rows_with_prompts:
            if self.skip_decorative and r.get("type") == "decorative":
                self._update_row(r["no"], status="skipped")
                skipped_decorative += 1
                continue
            candidates.append(r)

        # Step B: 選定。beat_mode は allocator が配分済み（candidates 全部）。
        # v2 は候補が max_diagrams 超なら均等間引き。
        if self.beat_mode:
            selected_nos = set(r["no"] for r in candidates)
        else:
            selected_nos = self._select_evenly_distributed(candidates, self.max_diagrams)
        self._log("generator",
                  f"画像配置方式: 全文均等配置 "
                  f"(候補 {len(candidates)} / 選定 {len(selected_nos)} / 上限 {self.max_diagrams})")

        # Step C: 各 row のステータスを「選定済み（pending）」or「間引き」にマーク
        generation_targets = []
        thinned_count = 0
        resumed_ai_done = 0
        for r in rows_with_prompts:
            no = r["no"]
            if self.skip_decorative and r.get("type") == "decorative":
                continue  # 既に skipped
            if no in selected_nos:
                # 再開時: 前回生成済みの画像はそのまま採用（API呼び出しゼロ）
                if self.resume:
                    fn = self._existing_image(no)
                    if fn:
                        self._update_row(no, status="ok", filename=fn)
                        resumed_ai_done += 1
                        continue
                # 画像 type はルーターの route を最優先（realphoto/map 等を確実に反映）
                route_type = routes.get(no, {}).get("route", "")
                img_type = route_type if route_type in AI_ROUTES else r.get("type", "illustration")
                generation_targets.append({
                    "index": no,
                    "prompt": r.get("prompt", ""),
                    "type": img_type,
                    "section": r.get("chapter_title", ""),
                    "chapter_index": r.get("chapter_index"),
                    "block_index": r.get("block_index"),
                    "excerpt": r.get("sentence", ""),
                    "block_text": r.get("block_text", ""),  # 検証の文脈用（前後段落）
                    "keypoint": r.get("sentence", "")[:30],
                    "allowed_terms": r.get("allowed_terms", []),
                    "diagram_blueprint": r.get("diagram_blueprint", {}),
                    "style": self.style_preset,
                    # キャラ固定: 先生が描かれる illustration のみ参照画像を使う
                    "character": bool(r.get("character", False)) and img_type == "illustration",
                })
            else:
                # 候補だったが均等配置から外れた → 「間引き」
                self._update_row(no, status="thinned")
                thinned_count += 1

        if resumed_ai_done:
            self._log("resume",
                      f"再開: 生成済みAI画像 {resumed_ai_done} 枚を再利用（残り {len(generation_targets)} 枚のみ生成）")

        # ===== v3 Step6: エンティティ参照（一貫性ロック）=====
        # 同じ被写体（国・組織・繰り返す概念）が 3 回以上 AI 画像に登場するとき、
        # 初出を canonical とし、後続は canonical 画像を参照に見た目を揃える。
        # beat_mode（v3 チャンネル）のときのみ。失敗しても生成は止めない。
        if self.beat_mode and generation_targets:
            try:
                from allocator import assign_entity_refs
                image_nos = [t["index"] for t in generation_targets]
                ent_assign = assign_entity_refs(image_nos, routes)
                if ent_assign:
                    targets_by_no = {t["index"]: t for t in generation_targets}
                    for no, a in ent_assign.items():
                        t = targets_by_no.get(no)
                        if not t:
                            continue
                        t["entity_role"] = a["role"]
                        t["entity_name"] = a["entity"]
                        if a["role"] == "follower":
                            t["entity_ref_of"] = a["canon_no"]
                    n_canon = sum(1 for v in ent_assign.values() if v["role"] == "canonical")
                    n_follow = sum(1 for v in ent_assign.values() if v["role"] == "follower")
                    n_kind = len({v["entity"] for v in ent_assign.values()})
                    self._log("generator",
                              f"エンティティ参照ロック: 初出 {n_canon} / 後続 {n_follow}（{n_kind} 種の繰り返し被写体）",
                              "後続は初出画像を参照して見た目を統一します")
            except Exception as e:
                self._log("generator", f"エンティティ参照の割当をスキップ（{str(e)[:80]}）")

        provider_label = self._provider_label(self.provider, self.openai_quality)
        active_providers = {
            self._provider_for_target(t) for t in generation_targets
        } if generation_targets else {self.provider}
        if len(active_providers) > 1:
            self._log(
                "generator",
                "タイプ別モデル生成を有効化",
                " / ".join(sorted(self._provider_label(p, self.openai_quality) for p in active_providers)),
            )
        # メモリ安全: 大量枚数のジョブは並列を控えめにして OOM/API失敗を避ける。
        # （同時に処理する画像が減るとピークメモリが下がる。安定優先で少し遅くなる。）
        n_gen = len(generation_targets)
        eff_concurrency = self.concurrency
        if n_gen > 120:
            eff_concurrency = min(eff_concurrency, 3)
        elif n_gen > 60:
            eff_concurrency = min(eff_concurrency, 3)
        if eff_concurrency != self.concurrency:
            self._log("generator",
                      f"メモリ保護のため並列を {self.concurrency} → {eff_concurrency} に調整（{n_gen} 枚）",
                      "大量生成時のOOM/API失敗を避けるため、安定優先で調整します")
        self._progress(3,
                       f"画像を並列生成中（{provider_label} / 同時 {eff_concurrency} 枚 / {n_gen} 枚）...",
                       40)
        self._log("generator",
                  f"{provider_label} で {n_gen} 枚を並列生成します",
                  f"スタイル: {self.style_preset}")

        generation_done_offset = 0
        current_provider_label = provider_label

        def on_item_event(info: dict):
            no = info.get("index", 0)
            status = info.get("status", "")
            update = {"status": status}
            if status == "ok":
                update["filename"] = info.get("filename")
            if info.get("error"):
                update["error"] = info["error"]
            self._update_row(no, **update)
            # 生成が1枚進むたびにプログレスバー(40→85%)とメッセージを更新し、
            # 長時間ジョブでも「止まって見えない」ようにする。
            if status in ("ok", "failed"):
                gt = n_gen
                done = generation_done_offset + (info.get("completed_total") or 0) + (info.get("failed_total") or 0)
                if gt:
                    pct = 40 + int(done / gt * 45)
                    self._progress(3,
                                   f"画像を並列生成中（{done}/{gt} 枚 / {current_provider_label}）...",
                                   min(85, pct))

        generation_batches = self._chunk_generation_targets(
            generation_targets,
            self.generation_batch_size,
            mode=self.generation_batch_mode,
        )
        if len(generation_batches) > 1:
            mode_label = "章ごと" if self.generation_batch_mode == "chapter" else "章/ブロック単位"
            self._log(
                "generator",
                f"{mode_label}の分割生成: {len(generation_batches)} バッチ",
                f"mode={self.generation_batch_mode} / batch_size={self.generation_batch_size}。各バッチ完了ごとに保存します",
            )

        results = []
        for batch_idx, batch_targets in enumerate(generation_batches, start=1):
            if len(generation_batches) > 1:
                first = batch_targets[0]
                label = first.get("section") or f"バッチ {batch_idx}"
                self._progress(
                    3,
                    f"画像生成 {batch_idx}/{len(generation_batches)}: {label}（{len(batch_targets)}枚）...",
                    min(84, 40 + int(generation_done_offset / max(1, n_gen) * 45)),
                )
                self._log(
                    "generator",
                    f"章/ブロック {batch_idx}/{len(generation_batches)} を生成中: {len(batch_targets)} 枚",
                    label,
                )

            provider_groups = []
            for target in batch_targets:
                target_provider = self._provider_for_target(target)
                if provider_groups and provider_groups[-1][0] == target_provider:
                    provider_groups[-1][1].append(target)
                else:
                    provider_groups.append((target_provider, [target]))

            batch_results = []
            for target_provider, provider_targets in provider_groups:
                current_provider_label = self._provider_label(target_provider, self.openai_quality)
                if len(provider_groups) > 1 or len(active_providers) > 1:
                    type_counts = {}
                    for t in provider_targets:
                        typ = t.get("type", "illustration")
                        type_counts[typ] = type_counts.get(typ, 0) + 1
                    self._log(
                        "generator",
                        f"{current_provider_label} で生成: {len(provider_targets)} 枚",
                        " / ".join(f"{k}:{v}" for k, v in sorted(type_counts.items())),
                    )

                provider_results = run_parallel_generation(
                    prompts=provider_targets,
                    output_dir=self.images_dir,
                    provider=target_provider,
                    gemini_api_key=gemini_key,
                    openai_api_key=openai_key,
                    openai_quality=self.openai_quality,
                    concurrency=eff_concurrency,
                    style_preset=self.style_preset,
                    progress_callback=on_item_event,
                    reference_image_path=self.character_ref_path,
                    realphoto_watermark=self.realphoto_watermark,
                )
                batch_results.extend(provider_results)
                generation_done_offset += len(provider_targets)
            results.extend(batch_results)
            if len(generation_batches) > 1:
                b_success = sum(1 for r in batch_results if r.get("success"))
                b_fail = len(batch_results) - b_success
                self._save_generation_checkpoint(batch_idx, len(generation_batches), batch_targets, batch_results)
                self._log(
                    "generator",
                    f"章/ブロック {batch_idx}/{len(generation_batches)} 完了・保存: 成功 {b_success} / 失敗 {b_fail}"
                )
                try:
                    import gc
                    gc.collect()
                except Exception:
                    pass

        new_success = sum(1 for r in results if r.get("success"))
        fail_count = len(results) - new_success
        success_count = new_success + resumed_ai_done
        self._log("generator",
                  f"画像生成完了: 成功 {success_count} / 失敗 {fail_count}"
                  + (f"（うち再開で再利用 {resumed_ai_done} 枚）" if resumed_ai_done else ""))

        # ===== Phase 3b: 図解の意味を自動検証 → ズレてたら再生成 =====
        # 検証は「あれば嬉しい」機能。何があってもジョブ完了を止めない（必ず先へ進む）。
        if self.verify_diagrams:
            # 生成フェーズのバッファを解放してから検証に入る（512MB環境のOOM対策）。
            import gc as _gc
            _gc.collect()
            theme = title
            _sum = analysis.get("summary", "")
            if _sum:
                theme = f"{title}（{_sum}）"
            try:
                self._verify_and_fix(results, generation_targets, gemini_key, openai_key, theme=theme)
            except Exception as e:
                self._log("error", f"検証フェーズをスキップしました（{str(e)[:80]}）。生成画像はそのまま使えます。")
            # イラスト/実写風は「⚠要確認フラグのみ」の軽量検品（自動再生成はしない）。
            # 編集者は⚠の行だけ目視→気になれば🔄すればよく、全数目視が不要になる。
            try:
                self._flag_check_ai_images(results, generation_targets, theme=theme)
            except Exception as e:
                self._log("verify", f"軽量検品をスキップ（{str(e)[:80]}）")

        # Web 検索の完了を待つ。
        # 通常チャンネルで長く待ちすぎると「画像生成は終わったのに止まった」ように見える。
        # 未取得分は下の AI 代替生成で穴埋めできるため、通常は短めに切り上げる。
        # 成功の法則(primary_media)だけは記事/一次資料を大量に探すので長めに待つ。
        if web_thread:
            self._progress(3, "Web 画像取得の完了を待機中...", 92)
            wait_minutes = 12 if self.web_search_profile == "primary_media" else 5
            self._log("websearch",
                      f"Web 画像取得の完了を最大 {wait_minutes} 分待機します...")
            waited = 0
            wait_seconds = wait_minutes * 60
            while web_thread.is_alive() and waited < wait_seconds:
                web_thread.join(timeout=30)
                waited += 30
                if web_thread.is_alive():
                    self._log(
                        "websearch",
                        f"Web 画像取得待機中: {min(waited, wait_seconds)}/{wait_seconds}秒"
                        f"（部分結果 {len(web_results_accumulator)} 件）"
                    )
            if web_thread.is_alive():
                self._log("warn",
                          f"Web 画像取得が {wait_minutes} 分以内に完了しませんでした。"
                          f"部分結果（{len(web_results_accumulator)} 件）で続行します。")

        # ===== Phase 3c: Web/Commons で拾えなかった画像を AI で穴埋め =====
        # ルーターが web_photo に振った行は通常 AI 生成から外れるため、Commons/Web が0件だと
        # 「待機」のまま画像枚数が大きく減る。画像化対象(display=image)なのにローカル画像が無い
        # 行だけを realphoto に降格して、AI実写風で代替生成する。
        web_fallback_results = []
        if self.route_mode == "auto" and web_photo_rows:
            missing_web_rows = []
            with self._rows_lock:
                state_by_no = {no: dict(st) for no, st in self._rows_state.items()}
            for r in web_photo_rows:
                if not self._wants_image(r):
                    continue
                st = state_by_no.get(r["no"], {})
                has_local_web = bool(st.get("web_local_file"))
                has_ai_file = bool(st.get("filename"))
                if has_local_web or has_ai_file:
                    continue
                rr = dict(r)
                rr["route"] = "realphoto"
                rr["route_reason"] = "Web画像取得失敗→AI実写風で代替"
                rr["engine"] = "ai"
                missing_web_rows.append(rr)

            if missing_web_rows:
                self._progress(3, f"Web未取得分をAI代替生成中（0/{len(missing_web_rows)}）...", 93)
                self._log(
                    "websearch",
                    f"Web/Commonsで取得できなかった {len(missing_web_rows)} 件をAI実写風で代替生成します",
                    "50枚指定時にWeb取得失敗分が待機のまま残る問題を防ぎます",
                )
                for r in missing_web_rows:
                    self._update_row(
                        r["no"],
                        route="realphoto",
                        route_reason=r["route_reason"],
                        engine="ai",
                        status="pending",
                        web_fallback=True,
                    )
                fallback_rows_for_prompts = self._attach_diagram_context(missing_web_rows, rows)
                fallback_prompts = generate_all_prompts(
                    client, fallback_rows_for_prompts, title=title,
                    user_instructions=self.user_instructions,
                    style_preset=self.style_preset, worldview_desc=self.worldview_desc,
                    max_workers=4, log=self._log,
                )
                self._remove_image_text_terms(fallback_prompts)
                fallback_targets = []
                for r in fallback_prompts:
                    self._update_row(
                        r["no"],
                        prompt=r.get("prompt", ""),
                        allowed_terms=r.get("allowed_terms", []),
                        type="realphoto",
                    )
                    fallback_targets.append({
                        "index": r["no"],
                        "prompt": r.get("prompt", ""),
                        "type": "realphoto",
                        "section": r.get("chapter_title", ""),
                        "excerpt": r.get("sentence", ""),
                        "block_text": r.get("block_text", ""),
                        "keypoint": r.get("sentence", "")[:30],
                        "allowed_terms": r.get("allowed_terms", []),
                        "style": self.style_preset,
                        "character": False,
                    })

                def on_web_fallback_event(info: dict):
                    no = info.get("index", 0)
                    status = info.get("status", "")
                    update = {"status": status}
                    if status == "ok":
                        update["filename"] = info.get("filename")
                    if info.get("error"):
                        update["error"] = info["error"]
                    self._update_row(no, **update)
                    if status in ("ok", "failed"):
                        gt = info.get("grand_total") or 0
                        done = (info.get("completed_total") or 0) + (info.get("failed_total") or 0)
                        if gt:
                            pct = 93 + int(done / gt * 4)
                            self._progress(3, f"Web未取得分をAI代替生成中（{done}/{gt}）...", min(97, pct))

                if fallback_targets:
                    fb_concurrency = min(eff_concurrency, 3)
                    web_fallback_results = run_parallel_generation(
                        prompts=fallback_targets,
                        output_dir=self.images_dir,
                        provider=self.provider,
                        gemini_api_key=gemini_key,
                        openai_api_key=openai_key,
                        openai_quality=self.openai_quality,
                        concurrency=fb_concurrency,
                        style_preset=self.style_preset,
                        progress_callback=on_web_fallback_event,
                        reference_image_path=self.character_ref_path,
                        realphoto_watermark=self.realphoto_watermark,
                    )
                    fb_success = sum(1 for r in web_fallback_results if r.get("success"))
                    fb_fail = len(web_fallback_results) - fb_success
                    success_count += fb_success
                    fail_count += fb_fail
                    results.extend(web_fallback_results)
                    self._log("websearch", f"AI代替生成完了: 成功 {fb_success} / 失敗 {fb_fail}")

        # ===== マニフェスト =====
        with self._rows_lock:
            final_rows = sorted(self._rows_state.values(), key=lambda x: x.get("no", 0))

        # rows_progress から Web URL がついた行数を再カウント（accumulator と二重チェック）
        web_count_from_rows = sum(1 for r in final_rows if r.get("web_source_url"))
        web_count_from_acc = len(web_results_accumulator)
        web_count_final = max(web_count_from_rows, web_count_from_acc)

        self._log("websearch",
                  f"Web 画像取得集計: accumulator={web_count_from_acc} / rows={web_count_from_rows}")

        generated_results = [r for r in results if r.get("success")]
        provider_counts = Counter(r.get("provider", "unknown") for r in generated_results)
        provider_failed_counts = Counter(r.get("provider", "unknown") for r in results if not r.get("success"))
        generated_type_counts = Counter(r.get("type", "unknown") for r in generated_results)
        route_counts = Counter((r.get("route") or "unknown") for r in final_rows)
        status_counts = Counter((r.get("status") or "pending") for r in final_rows)
        engine_counts = Counter((r.get("engine") or "unknown") for r in final_rows)
        rendered_ok_count = sum(
            1 for r in final_rows
            if r.get("engine") == "render" and r.get("status") == "ok" and r.get("filename")
        )
        web_local_count = sum(1 for r in final_rows if r.get("web_local_file"))
        ai_image_count = len(generated_results)

        cost_audit = {
            "channel_id": self.channel_id,
            "base_provider": self.provider,
            "type_providers": self.type_providers,
            "openai_quality": self.openai_quality if self.provider == PROVIDER_GPT_IMAGE else None,
            "chart_engine": self.chart_engine,
            "allow_charts": self.allow_charts,
            "map_engine": self.map_engine,
            "intro_visual_boost": self.intro_visual_boost,
            "map_route_limit": self.map_route_limit,
            "realistic_route_min": self.realistic_route_min,
            "no_image_text": self.no_image_text,
            "photo_source": self.photo_source,
            "web_search_profile": self.web_search_profile,
            "verify_diagrams": self.verify_diagrams,
            "provider_generated_counts": dict(provider_counts),
            "provider_failed_counts": dict(provider_failed_counts),
            "generated_type_counts": dict(generated_type_counts),
            "route_counts": dict(route_counts),
            "status_counts": dict(status_counts),
            "engine_counts": dict(engine_counts),
            "rendered_ok_count": rendered_ok_count,
            "ai_image_count": ai_image_count,
            "web_results_count": web_count_final,
            "web_local_count": web_local_count,
            "web_fallback_generated": sum(1 for r in web_fallback_results if r.get("success")),
            "optimization_notes": {
                "programmatic_render_saved_images": rendered_ok_count,
                "commons_or_web_images_saved_ai": web_local_count,
                "verify_model": "claude-haiku-4-5" if self.verify_diagrams else "",
            },
        }

        # ===== 制作資料パック（sources.html / sources.md）を書き出す =====
        has_source_pack = False
        if self.build_source_pack:
            # 元ネタ動画の収集完了を待つ（最大5分・ベストエフォート）
            if not source_videos_done.wait(timeout=300):
                self._log("sources", "元ネタ動画の収集が終わらないため、集まった分だけで資料を作ります")
            try:
                with self._rows_lock:
                    rows_for_pack = sorted(self._rows_state.values(), key=lambda x: x.get("no", 0))
                self._write_source_pack(
                    title, chapters, rows_for_pack, source_videos,
                    list(web_results_accumulator),
                )
                has_source_pack = True
                self._log("sources", "制作資料パック（sources.html / sources.md）を書き出しました")
            except Exception as e:
                self._log("sources", f"資料パックの書き出しに失敗（{str(e)[:80]}）")

        manifest = {
            "title": title,
            "summary": analysis.get("summary", ""),
            "keywords": analysis.get("keywords", []),
            "has_source_pack": has_source_pack,
            "user_instructions": self.user_instructions,
            "provider": self.provider,
            "openai_quality": self.openai_quality if self.provider == PROVIDER_GPT_IMAGE else None,
            "type_providers": self.type_providers,
            "style_preset": self.style_preset,
            "channel_id": self.channel_id,
            "route_mode": self.route_mode,
            "concurrency": self.concurrency,
            "total_sentences": total_sentences,
            "max_diagrams": self.max_diagrams,
            "generation_batch_size": self.generation_batch_size,
            "generation_batch_mode": self.generation_batch_mode,
            "web_image_count": self.web_image_count,
            "ai_route_count": len(ai_rows),
            "web_photo_count": len(web_photo_rows),
            "skip_route_count": len(skip_rows),
            "generated": success_count,
            "failed": fail_count,
            "web_fallback_generated": sum(1 for r in web_fallback_results if r.get("success")),
            "skipped_decorative": skipped_decorative,
            "thinned": thinned_count,  # 均等配置のため間引かれた数
            "web_results_count": web_count_final,
            "cost_audit": cost_audit,
            "rows": final_rows,
            "chapters": [{"title": c["title"], "block_count": len(c["blocks"])} for c in chapters],
            "completed_at": datetime.now().isoformat(),
        }
        save_json(self.output_dir / "manifest.json", manifest)

        # CSV
        self._write_csv(self.output_dir / "result.csv", final_rows)

        # HTML ギャラリー（画像を埋め込んだ表。開けばぱっと全体を見渡せる）
        self._write_gallery(self.output_dir / "result.html", final_rows, title)

        self._progress(4, f"完了: 図解 {success_count} / Web {web_count_final} / 全 {total_sentences} 文", 100)
        return manifest

    def _write_gallery(self, path: Path, rows: list, title: str):
        """画像を埋め込んだ HTML ギャラリーを出力（相対パスで自己完結）"""
        import html as _html

        def cell_img(r):
            fn = r.get("filename") or r.get("web_local_file") or ""
            if fn:
                return f'<img src="images/{_html.escape(fn)}" loading="lazy">'
            st = r.get("status", "")
            if st == "skipped" or r.get("route") == "skip":
                return '<span class="no">—（スキップ）</span>'
            return '<span class="no">（画像なし）</span>'

        parts = [f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_html.escape(title)} - 画像ギャラリー</title>
<style>
 body{{font-family:'Hiragino Sans','Noto Sans JP',sans-serif;margin:0;padding:24px;background:#f8fafc;color:#1f2937}}
 h1{{font-size:20px;margin:0 0 16px}}
 table{{border-collapse:collapse;width:100%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
 th,td{{border:1px solid #e5e7eb;padding:8px;vertical-align:top;font-size:13px}}
 th{{background:#f1f5f9;position:sticky;top:0;text-align:left}}
 td.no{{text-align:center;color:#9ca3af;font-family:monospace;width:48px}}
 td.chap{{font-weight:600;color:#0e7490;width:120px}}
 td.sent{{max-width:340px}}
 td.img{{width:340px;text-align:center}}
 td.img img{{max-width:320px;max-height:180px;border-radius:6px;border:1px solid #e5e7eb}}
 .src{{display:inline-block;font-size:11px;padding:1px 6px;border-radius:4px;background:#eef2ff;color:#4338ca}}
 .no{{color:#cbd5e1;font-size:12px}}
 a{{color:#7c3aed}}
</style></head><body>
<h1>{_html.escape(title)} — 画像ギャラリー（全 {len(rows)} 文）</h1>
<table><thead><tr>
<th>№</th><th>章</th><th>センテンス</th><th>ソース</th><th>画像</th>
</tr></thead><tbody>"""]

        # 冒頭の固定画像（あれば先頭に）
        def _find_fixed(slot):
            for ext in (".png", ".jpg", ".jpeg", ".webp"):
                if (self.images_dir / f"{slot}{ext}").exists():
                    return f"{slot}{ext}"
            return None
        intro_fn = _find_fixed("intro")
        outro_fn = _find_fixed("outro")
        if intro_fn:
            parts.append(
                f'<tr style="background:#ecfeff"><td class="no">▶</td><td class="chap">冒頭固定</td>'
                f'<td class="sent">（差し込み画像・冒頭）</td><td><span class="src">固定</span></td>'
                f'<td class="img"><img src="images/{intro_fn}" loading="lazy"></td></tr>'
            )

        last_chap = None
        for r in rows:
            chap = r.get("chapter_title", "")
            chap_show = chap if chap != last_chap else ""
            last_chap = chap
            route_label = self.ROUTE_LABELS.get(r.get("route", ""), r.get("route", ""))
            web_link = ""
            if r.get("web_source_url"):
                web_link = f'<br><a href="{_html.escape(r["web_source_url"])}" target="_blank">出典</a>'
            parts.append(
                f'<tr><td class="no">{r.get("no","")}</td>'
                f'<td class="chap">{_html.escape(chap_show)}</td>'
                f'<td class="sent">{_html.escape(r.get("sentence",""))}</td>'
                f'<td><span class="src">{_html.escape(route_label)}</span>{web_link}</td>'
                f'<td class="img">{cell_img(r)}</td></tr>'
            )
        # 終わりの固定画像（あれば末尾に）
        if outro_fn:
            parts.append(
                f'<tr style="background:#fef2f2"><td class="no">■</td><td class="chap">終わり固定</td>'
                f'<td class="sent">（差し込み画像・終わり/CTA）</td><td><span class="src">固定</span></td>'
                f'<td class="img"><img src="images/{outro_fn}" loading="lazy"></td></tr>'
            )
        parts.append("</tbody></table></body></html>")
        try:
            path.write_text("\n".join(parts), encoding="utf-8")
        except Exception as e:
            self._log("warn", f"ギャラリー出力に失敗: {str(e)[:80]}")

    # ルート → 日本語ラベル
    ROUTE_LABELS = {
        "web_photo": "Web写真",
        "realphoto": "実写風",
        "map": "地図",
        "diagram": "図解",
        "chart": "グラフ",
        "illustration": "イラスト",
        "skip": "スキップ",
        "": "",
    }

    def _render_charts(self, render_rows):
        """chart_spec を matplotlib で 1920x1080 PNG に描画（engine:render・LLM不使用）。

        数値の狂い・文字化けが構造的にゼロ。描画失敗は diagram(engine:ai) へ降格する。
        """
        try:
            from renderer import render_chart
        except Exception as e:
            self._log("error", f"renderer 読込失敗（{str(e)[:60]}）。chart は AI 生成へ降格。")
            for r in render_rows:
                r["route"] = "diagram"
                r["engine"] = "ai"
                self._update_row(r["no"], route="diagram", engine="ai")
            return
        done = 0
        for r in render_rows:
            no = r["no"]
            self._update_row(no, status="generating", engine="render")
            ok = render_chart(r.get("chart_spec"), self.images_dir / f"{no}.png", theme=self.chart_theme)
            if ok:
                # chart_spec を保存しておく（再生成で確実に同じグラフを描き直すため）
                self._update_row(no, status="ok", filename=f"{no}.png", engine="render",
                                 chart_spec=r.get("chart_spec"))
                done += 1
            else:
                r["route"] = "diagram"
                r["engine"] = "ai"
                self._update_row(no, route="diagram", engine="ai")
        self._log("renderer", f"chart レンダリング完了: {done} 枚（決定論・文字化けゼロ）")

    def _render_maps(self, render_rows):
        """map_spec を Natural Earth GeoJSON + matplotlib で描画（engine:render・LLM不使用）。

        国境が正確（AI の航空写真風のデタラメ国境を排除）。失敗は illustration(ai) へ降格。
        """
        try:
            from renderer import render_map
        except Exception as e:
            self._log("error", f"renderer 読込失敗（{str(e)[:60]}）。map は AI 生成へ降格。")
            for r in render_rows:
                r["route"] = "illustration"
                r["engine"] = "ai"
                self._update_row(r["no"], route="illustration", engine="ai")
            return
        done = 0
        for r in render_rows:
            no = r["no"]
            self._update_row(no, status="generating", engine="render")
            ok = render_map(r.get("map_spec"), self.images_dir / f"{no}.png", theme=self.chart_theme)
            if ok:
                # map_spec を保存しておく（再生成で確実に同じ地図を描き直すため）
                self._update_row(no, status="ok", filename=f"{no}.png", engine="render",
                                 map_spec=r.get("map_spec"))
                done += 1
            else:
                r["route"] = "illustration"
                r["engine"] = "ai"
                self._update_row(no, route="illustration", engine="ai")
        self._log("renderer", f"map レンダリング完了: {done} 枚（正確な国境）")

    def _verify_and_fix(self, results, generation_targets, gemini_key, openai_key, theme=""):
        """生成済み diagram/chart を Claude Vision で検証し、ズレてたら1回だけ再生成する。

        検証は「あれば嬉しい」機能なので、絶対にジョブ完了をブロックしない:
        - 全体に時間予算（budget）を設け、超過したら残りはスキップして先へ進む
        - 1 枚ごとにもタイムアウト（固まった検証で全体が止まらない）
        - チャンネル別 Anthropic キーを使い、リトライを抑えて素早く諦める
        """
        import time as _time
        from concurrent.futures import (
            ThreadPoolExecutor, as_completed, TimeoutError as _FutTimeout)
        from verifier import verify_image, DEFAULT_VERIFY_TYPES

        # チャンネル別 Anthropic キーを使う。固まっても素早く諦めるためリトライ抑制。
        try:
            client = get_anthropic_client(self.anthropic_key).with_options(
                max_retries=1, timeout=60.0)
        except Exception as e:
            self._log("verify", f"検証をスキップ（APIクライアント初期化失敗: {str(e)[:60]}）")
            return

        # 検証対象: 生成成功した diagram / chart（targets と results を突合）
        targets_by_no = {t["index"]: t for t in generation_targets}
        verify_list = []
        for r in results:
            if not r.get("success"):
                continue
            t = targets_by_no.get(r.get("index"))
            if t and t.get("type") in DEFAULT_VERIFY_TYPES:
                verify_list.append(t)

        if not verify_list:
            return

        # 時間予算: 1 枚 ~12 秒 ÷ 4 並列 を目安に、最大 10 分。超えたら打ち切って先へ進む。
        budget = min(600, max(90, int(len(verify_list) / 4 * 12) + 60))
        self._progress(3, f"図解の意味を検証中（0/{len(verify_list)} 枚）...", 90)
        self._log("verify",
                  f"diagram/chart {len(verify_list)} 枚の意味を Claude Vision で検証します"
                  f"（最大 {budget // 60} 分・超過分はスキップ）")

        # 並列で検証（原稿の文脈＝テーマ・章・前後段落 を渡す）
        def _do_verify(t):
            no = t["index"]
            img_path = self.images_dir / f"{no}.png"
            v = verify_image(
                client, img_path, t.get("excerpt", ""), t.get("type", "diagram"),
                allowed_terms=t.get("allowed_terms"),
                block_context=t.get("block_text", ""),
                chapter=t.get("section", ""),
                theme=theme,
                diagram_blueprint=t.get("diagram_blueprint", {}),
            )
            return (t, v)

        ng = []
        checked = 0
        timed_out = False
        deadline = _time.monotonic() + budget
        # 並列は 3 に抑える（縮小済み画像 + 512MB 環境でのピークメモリ削減）
        ex = ThreadPoolExecutor(max_workers=3)
        try:
            futs = {ex.submit(_do_verify, t): t for t in verify_list}
            try:
                for f in as_completed(futs, timeout=budget):
                    remaining = deadline - _time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    try:
                        t, v = f.result(timeout=max(1.0, remaining))
                        checked += 1
                        if checked % 20 == 0:
                            self._progress(3, f"図解の意味を検証中（{checked}/{len(verify_list)} 枚）...", 90)
                        if not v.get("ok"):
                            ng.append((t, v))
                            self._update_row(
                                t["index"],
                                verify_issue=True,
                                verify_reason=v.get("reason", ""),
                                verify_issue_tags=v.get("issue_tags", []),
                            )
                            self._log("verify", f"№{t['index']} 要修正: {v.get('reason','')}")
                    except _FutTimeout:
                        timed_out = True
                        break
                    except Exception as e:
                        self._log("error", f"検証エラー: {str(e)[:80]}")
            except _FutTimeout:
                timed_out = True
        finally:
            # 残りの未実行タスクはキャンセル。実行中スレッドは待たずに先へ進む。
            ex.shutdown(wait=False, cancel_futures=True)
            import gc as _gc
            _gc.collect()  # 検証で使った画像バッファを解放

        if timed_out:
            self._log("verify",
                      f"検証は時間上限({budget // 60}分)に達したため打ち切りました"
                      f"（{checked}/{len(verify_list)} 枚確認）。生成画像はそのまま使えます。")

        if not ng:
            if not timed_out:
                self._log("verify", "検証完了: 全て意味OK ✓")
            return

        # 改善指示を付けて再生成（1回）
        self._log("verify", f"{len(ng)} 枚を改善指示付きで再生成します")
        fix_targets = []
        for t, v in ng:
            entry = dict(t)
            hint = v.get("fix_hint", "")
            if hint:
                entry["prompt"] = f"{t.get('prompt','')}\n\nIMPROVE: {hint}"
            self._update_row(t["index"], status="generating", verify_issue=True)
            fix_targets.append(entry)

        def on_fix_event(info):
            no = info.get("index", 0)
            st = info.get("status", "")
            upd = {"status": st}
            if st == "ok":
                upd["filename"] = info.get("filename")
                upd["verify_fixed"] = True
                upd["verify_issue"] = False
            self._update_row(no, **upd)

        run_parallel_generation(
            prompts=fix_targets,
            output_dir=self.images_dir,
            provider=self.provider,
            gemini_api_key=gemini_key,
            openai_api_key=openai_key,
            openai_quality=self.openai_quality,
            concurrency=self.concurrency,
            style_preset=self.style_preset,
            progress_callback=on_fix_event,
            reference_image_path=self.character_ref_path,
            realphoto_watermark=self.realphoto_watermark,
        )
        self._log("verify", f"再生成完了（{len(fix_targets)} 枚を作り直しました）")

    def _flag_check_ai_images(self, results, generation_targets, theme=""):
        """イラスト/実写風の軽量検品（⚠フラグのみ・自動再生成しない）。

        diagram/chart は _verify_and_fix が修正まで行うのに対し、こちらは Haiku で
        「文とズレていないか・文字化けが無いか」を判定して verify_issue を立てるだけ。
        既存UIの「失敗・要確認だけ」フィルタ／章バッジがそのまま拾う。
        時間予算つき・失敗してもジョブを止めない。
        """
        import time as _time
        from concurrent.futures import (
            ThreadPoolExecutor, as_completed, TimeoutError as _FutTimeout)
        from verifier import verify_image

        flag_types = ("illustration", "realphoto")
        targets_by_no = {t["index"]: t for t in generation_targets}
        checks = []
        for r in results:
            if not r.get("success") or not r.get("filename"):
                continue
            t = targets_by_no.get(r.get("index"))
            if not t or t.get("type") not in flag_types:
                continue
            checks.append((r, t))
        if not checks:
            return
        try:
            client = get_anthropic_client(self.anthropic_key).with_options(
                max_retries=1, timeout=60.0)
        except Exception as e:
            self._log("verify", f"軽量検品をスキップ（APIクライアント初期化失敗: {str(e)[:60]}）")
            return

        budget = min(480.0, max(90.0, len(checks) * 4.0))
        deadline = _time.monotonic() + budget
        self._log("verify",
                  f"軽量検品（⚠フラグのみ・再生成なし）: {len(checks)} 枚（illustration/realphoto）")

        def _do_check(pair):
            r, t = pair
            v = verify_image(
                client, self.images_dir / r["filename"], t.get("excerpt", ""),
                img_type=t.get("type", "illustration"),
                allowed_terms=t.get("allowed_terms", []),
                block_context=t.get("block_text", ""),
                chapter=t.get("section", ""), theme=theme,
            )
            return t, v

        flagged = []
        checked = 0
        ex = ThreadPoolExecutor(max_workers=3)
        try:
            futs = {ex.submit(_do_check, p): p for p in checks}
            try:
                for f in as_completed(futs, timeout=budget):
                    remaining = deadline - _time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        t, v = f.result(timeout=max(1.0, remaining))
                        checked += 1
                        if not v.get("ok", True):
                            flagged.append(t["index"])
                            self._update_row(
                                t["index"],
                                verify_issue=True,
                                verify_reason=v.get("reason", ""),
                                verify_issue_tags=v.get("issue_tags", []),
                            )
                    except _FutTimeout:
                        break
                    except Exception as e:
                        self._log("error", f"軽量検品エラー: {str(e)[:80]}")
            except _FutTimeout:
                pass
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
            import gc as _gc
            _gc.collect()

        self._log("verify",
                  f"軽量検品完了: ⚠{len(flagged)} 枚 / 確認 {checked}/{len(checks)} 枚"
                  + (f"（要確認 №{', '.join(map(str, flagged[:10]))}）" if flagged else ""))

    def _write_source_pack(self, title, chapters, rows, source_videos, web_infos):
        """制作資料パック（sources.html / sources.md）を書き出す。

        章ごとに「元ネタ・参考動画」「一次資料・記事（採用写真の出典＋未採用候補）」
        「採用した実写・Web写真」をまとめ、編集者が“この資料だけで”裏取り・素材差し替え・
        演出参考までできる形にする。
        """
        import html as _html

        by_ch_rows: dict = {}
        for r in rows:
            ci = r.get("chapter_index")
            if ci is None:
                continue
            by_ch_rows.setdefault(ci, []).append(r)
        info_by_no: dict = {}
        for it in web_infos:
            no = it.get("no")
            if no is not None and no not in info_by_no:
                info_by_no[no] = it

        _MT_LABEL = {"scene": "場面", "person": "人物", "document": "資料", "data": "データ"}

        md: list = [f"# 制作資料パック: {title}",
                    "",
                    f"生成: {datetime.now().strftime('%Y-%m-%d %H:%M')} / センテンスつくーる",
                    "",
                    "元ネタ動画は裏取り・演出参考用です（動画内への転用は権利確認のうえで）。",
                    ""]
        hsec: list = []

        for ci, ch in enumerate(chapters):
            ch_title = ch.get("title", f"第{ci + 1}章")
            ch_rows = by_ch_rows.get(ci, [])
            vids = source_videos.get(ci, []) or []
            web_rows = [r for r in ch_rows if r.get("web_source_url")]
            if not vids and not web_rows:
                continue

            md.append(f"## {ch_title}")
            h_items: list = [f"<h2>{_html.escape(ch_title)}</h2>"]

            if vids:
                md.append("")
                md.append("### 🎬 元ネタ・参考動画")
                h_items.append("<h3>🎬 元ネタ・参考動画</h3><ul>")
                for v in vids:
                    mark = "" if v.get("verified") else "（URL要確認）"
                    md.append(f"- [{v.get('title') or v['url']}]({v['url']}) — {v.get('reason', '')}{mark}")
                    h_items.append(
                        f"<li><a href='{_html.escape(v['url'])}' target='_blank' rel='noopener'>"
                        f"{_html.escape(v.get('title') or v['url'])}</a>"
                        f" <span class='reason'>{_html.escape(v.get('reason', ''))}{mark}</span></li>")
                h_items.append("</ul>")

            if web_rows:
                md.append("")
                md.append("### 📄 一次資料・記事と採用写真")
                h_items.append("<h3>📄 一次資料・記事と採用写真</h3><ul>")
                for r in web_rows:
                    no = r.get("no")
                    src = r.get("web_source_url", "")
                    stitle = r.get("web_source_title") or src
                    mt = _MT_LABEL.get(r.get("web_material_type", ""), "")
                    mt_tag = f"［{mt}］" if mt else ""
                    photo = r.get("web_local_file") or ""
                    photo_note = f" / 採用写真: images/{photo}" if photo else ""
                    sent = (r.get("sentence") or "")[:40]
                    md.append(f"- №{no} {mt_tag}[{stitle}]({src}) — 「{sent}」{photo_note}")
                    img_html = (f"<img src='images/{_html.escape(photo)}' loading='lazy'>" if photo else "")
                    h_items.append(
                        f"<li>{img_html}<div><b>№{no}</b> {mt_tag}"
                        f"<a href='{_html.escape(src)}' target='_blank' rel='noopener'>{_html.escape(stitle)}</a>"
                        f"<div class='sent'>「{_html.escape(sent)}」{_html.escape(photo_note)}</div>")
                    # 未採用候補（差し替え用の控え）
                    cands = [c for c in (r.get("web_candidates") or []) if c.get("url") and c.get("url") != src][:3]
                    if cands:
                        md.append(f"  - 差し替え候補: " + " / ".join(f"[{c.get('title') or c['url']}]({c['url']})" for c in cands))
                        h_items.append("<div class='cands'>差し替え候補: " + " / ".join(
                            f"<a href='{_html.escape(c['url'])}' target='_blank' rel='noopener'>{_html.escape(c.get('title') or c['url'])}</a>"
                            for c in cands) + "</div>")
                    h_items.append("</div></li>")
                h_items.append("</ul>")

            md.append("")
            hsec.append("\n".join(h_items))

        (self.output_dir / "sources.md").write_text("\n".join(md), encoding="utf-8")

        html_doc = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<title>制作資料パック - {_html.escape(title)}</title>
<style>
body{{font-family:'Hiragino Sans','Noto Sans JP',sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#1f2937;background:#fafaf8}}
h1{{font-size:22px;border-bottom:3px solid #0f766e;padding-bottom:8px}}
h2{{font-size:18px;margin-top:28px;background:#0f766e;color:#fff;padding:6px 12px;border-radius:8px}}
h3{{font-size:14px;margin:14px 0 6px;color:#0f766e}}
ul{{list-style:none;padding:0;margin:0}}
li{{display:flex;gap:10px;align-items:flex-start;padding:8px;border-bottom:1px solid #e5e7eb}}
li img{{width:120px;height:68px;object-fit:cover;border-radius:6px;flex-shrink:0}}
a{{color:#0e7490;text-decoration:none}} a:hover{{text-decoration:underline}}
.sent{{color:#6b7280;font-size:12px;margin-top:2px}}
.cands{{color:#9333ea;font-size:11px;margin-top:2px}}
.reason{{color:#6b7280;font-size:12px}}
.note{{color:#92400e;background:#fef3c7;padding:8px 12px;border-radius:8px;font-size:12px}}
</style></head><body>
<h1>📚 制作資料パック: {_html.escape(title)}</h1>
<p class="note">元ネタ動画は裏取り・演出参考用です（動画内への転用は権利確認のうえで）。
写真の差し替えは進捗ページの「🖼候補」からワンクリックでできます。</p>
{''.join(hsec)}
<p style="color:#9ca3af;font-size:11px;margin-top:24px">生成: {datetime.now().strftime('%Y-%m-%d %H:%M')} / センテンスつくーる</p>
</body></html>"""
        (self.output_dir / "sources.html").write_text(html_doc, encoding="utf-8")

    def _write_csv(self, path: Path, rows: list):
        """CSV を書き出す（スプレッドシートと同構造）"""
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            # v3: ビート/推定開始/エンジン/重要度/表示/ライセンス/クレジット 列を追加
            w.writerow(["章", "ブロック", "センテンス", "№", "ビート", "推定開始", "ソース",
                        "エンジン", "重要度", "表示", "検品", "画像", "URL", "URL種別", "ライセンス", "クレジット"])
            _disp = {"image": "画像", "hold": "継続", "none": "なし"}
            for r in rows:
                block_text = ""
                if r.get("sentence_index") == 0:
                    block_text = r.get("block_text", "")
                chapter = ""
                if r.get("block_index") == 0 and r.get("sentence_index") == 0:
                    chapter = r.get("chapter_title", "")
                route_label = self.ROUTE_LABELS.get(r.get("route", ""), r.get("route", ""))
                beat = r.get("beat_id")
                disp_label = _disp.get(r.get("display", ""), "")
                if not disp_label and r.get("filename"):
                    disp_label = "画像"  # v2(beat_mode無し)でも画像があれば「画像」
                w.writerow([
                    chapter,
                    block_text,
                    r.get("sentence", ""),
                    r.get("no", ""),
                    "" if beat is None else beat,
                    r.get("est_start", "") or "",
                    route_label,
                    r.get("engine", "") or "",
                    r.get("importance", "") or "",
                    disp_label,
                    ("⚠ " + (r.get("verify_reason") or "要確認")) if r.get("verify_issue") else "",
                    r.get("filename", "") or "",
                    r.get("web_source_url", "") or r.get("commons_page_url", "") or "",
                    r.get("web_source_type", "") or "",
                    r.get("license", "") or "",
                    r.get("attribution", "") or "",
                ])
