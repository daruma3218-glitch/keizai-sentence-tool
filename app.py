#!/usr/bin/env python3
"""センテンスつくーる - Flask Web アプリ

原稿をセンテンス単位に分割して、各文に対応する図解を一括生成する。
出力: テーブル表示（Web）+ CSV ダウンロード（Excel / Sheets 用）
"""

import functools
import io
import csv
import json
import os
import secrets
import shutil
import threading
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)

from utils import load_env, load_json, SNAPSHOT_IO_LOCK
from pipeline import SentencePipeline, VALID_STYLES
from generator import PROVIDER_NANOBANANA, PROVIDER_GPT_IMAGE, VALID_PROVIDERS


PROJECT_ROOT = Path(__file__).parent


def _resolve_output_root() -> tuple[Path, str, bool]:
    """ジョブ出力の保存ルートを決める。

    Render ではアプリ直下のファイルは再デプロイ/再起動で消えるため、
    永続ディスク（通常 /data）を優先する。DATA_DIR を設定し忘れても
    /data がマウントされていれば自動で使う。
    """
    data_dir = os.environ.get("DATA_DIR", "").strip()
    if data_dir:
        root = Path(data_dir)
        return root, "DATA_DIR", root.exists()
    render_disk = Path("/data")
    if render_disk.exists() and os.access(str(render_disk), os.W_OK):
        return render_disk, "auto:/data", True
    return PROJECT_ROOT, "project", False


OUTPUT_ROOT, OUTPUT_STORAGE_MODE, OUTPUT_IS_PERSISTENT = _resolve_output_root()
OUTPUT_DIR = OUTPUT_ROOT / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

load_env(PROJECT_ROOT)


def load_channels() -> list:
    """channels.json を読み込みチャンネル一覧を返す（無ければ default 1件）。"""
    data = load_json(PROJECT_ROOT / "channels.json", {})
    chans = data.get("channels", []) if isinstance(data, dict) else []
    if not chans:
        chans = [{"id": "default", "name": "共通（デフォルト）", "api_env_prefix": "", "defaults": {}}]
    return chans


def get_channel(channel_id: str) -> dict:
    for c in load_channels():
        if c.get("id") == channel_id:
            return c
    return load_channels()[0]


def resolve_channel_keys(channel: dict) -> dict:
    """チャンネルの api_env_prefix から各APIキーを解決（無ければ共通キーにフォールバック）。"""
    prefix = (channel.get("api_env_prefix") or "").strip()
    def pick(name):
        if prefix:
            v = os.environ.get(f"{prefix}_{name}", "").strip()
            if v:
                return v
        return os.environ.get(name, "").strip()
    return {
        "anthropic": pick("ANTHROPIC_API_KEY"),
        "gemini": pick("GEMINI_API_KEY"),
        "openai": pick("OPENAI_API_KEY"),
    }


def _resolve_secret_key() -> str:
    """安定した SECRET_KEY を取得する。

    優先順: 環境変数 SECRET_KEY → 永続ファイル(.secret_key) → 新規生成して永続化。
    こうすることでサーバー再起動やワーカー間でも同じ鍵を使い、
    セッション（ログイン状態）が無効化されない。
    """
    env_key = os.environ.get("SECRET_KEY", "").strip()
    if env_key:
        return env_key
    key_file = (OUTPUT_ROOT if OUTPUT_IS_PERSISTENT else PROJECT_ROOT) / ".secret_key"
    try:
        if key_file.exists():
            saved = key_file.read_text(encoding="utf-8").strip()
            if saved:
                return saved
        new_key = secrets.token_hex(32)
        key_file.write_text(new_key, encoding="utf-8")
        return new_key
    except Exception:
        # ファイルに書けない環境では一応ランダム（最終手段）
        return secrets.token_hex(32)


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB
app.secret_key = _resolve_secret_key()
# セッションを永続化（ブラウザを閉じても・長時間ダウンロード中でも切れない）
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=14)

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

# ジョブ状態（メモリ）
_jobs: dict = {}
_job_logs: dict = {}
_jobs_lock = threading.Lock()
# route_feedback.jsonl への追記を直列化（同時フィードバックでも行が壊れないように）
FEEDBACK_LOCK = threading.Lock()


def _safe_job_dir(job_id: str):
    """OUTPUT_DIR 直下のジョブディレクトリだけを返す。"""
    if not job_id or "/" in job_id or "\\" in job_id or job_id in (".", ".."):
        return None
    job_dir = (OUTPUT_DIR / job_id).resolve()
    try:
        job_dir.relative_to(OUTPUT_DIR.resolve())
    except ValueError:
        return None
    return job_dir


def _safe_download_name(text: str, fallback: str = "download") -> str:
    return "".join(c for c in (text or "") if c not in r'\/:*?"<>|').strip()[:50] or fallback


# ====== 認証 ======
def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not APP_PASSWORD:
            return f(*args, **kwargs)
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/version")
def version():
    """Render が実際にどの版を読んでいるか確認するための軽量診断。"""
    upload_html = ""
    try:
        upload_html = (PROJECT_ROOT / "templates" / "upload.html").read_text(encoding="utf-8")
    except Exception:
        upload_html = ""
    return jsonify({
        "service": "keizai-sentence-tool",
        "git_commit": os.environ.get("RENDER_GIT_COMMIT", ""),
        "service_name": os.environ.get("RENDER_SERVICE_NAME", ""),
        "template_has_style_preset_input": 'name="style_preset"' in upload_html,
        "template_has_diagram_style_text": "図解スタイル" in upload_html,
        "output_dir": str(OUTPUT_DIR),
        "output_storage_mode": OUTPUT_STORAGE_MODE,
        "output_is_persistent": OUTPUT_IS_PERSISTENT,
        "data_dir_env": os.environ.get("DATA_DIR", ""),
        "checked_at": datetime.now().isoformat(),
    })


@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD:
        return redirect(url_for("index"))
    if session.get("authenticated"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if request.form.get("password", "") == APP_PASSWORD:
            session.permanent = True  # 14日間有効（PERMANENT_SESSION_LIFETIME）
            session["authenticated"] = True
            return redirect(url_for("index"))
        error = "パスワードが正しくありません"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ====== ジョブ管理 ======
def _set_job_state(job_id: str, **kwargs):
    with _jobs_lock:
        state = _jobs.setdefault(job_id, {})
        state.update(kwargs)
        state["updated_at"] = datetime.now().isoformat()
        try:
            (OUTPUT_DIR / job_id / "job.json").write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass


def _get_job_state(job_id: str) -> dict:
    with _jobs_lock:
        if job_id in _jobs:
            return dict(_jobs[job_id])
    job_path = OUTPUT_DIR / job_id / "job.json"
    if job_path.exists():
        try:
            return json.loads(job_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _add_log(job_id: str, category: str, message: str, detail: str = ""):
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "category": category,
        "message": message,
        "detail": detail,
    }
    with _jobs_lock:
        logs = _job_logs.setdefault(job_id, [])
        logs.append(entry)
    try:
        (OUTPUT_DIR / job_id / "logs.json").write_text(
            json.dumps(_job_logs[job_id], ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def _run_pipeline_thread(job_id: str, manuscript_text: str, user_instructions: str,
                         concurrency: int, provider: str, openai_quality: str,
                         skip_decorative: bool, style_preset: str,
                         web_image_count: int, max_diagrams: int, route_mode: str,
                         worldview_desc: str = "", verify_diagrams: bool = True,
                         channel_id: str = "default", ch_keys: dict = None,
                         character_ref_path: str = "",
                         title_override: str = "", fact_context: str = "",
                         resume: bool = False):
    job_dir = OUTPUT_DIR / job_id
    ch_keys = ch_keys or {}
    provider_label = ("nanobanana (Gemini)" if provider == PROVIDER_NANOBANANA
                      else f"gpt-image ({openai_quality})")
    try:
        _set_job_state(job_id, status="running", phase=0,
                       message="再開しています..." if resume else "開始しています...", percent=0)
        _add_log(job_id, "system",
                 ("ジョブを再開します（完了済みの成果物は再利用）: " if resume else "ジョブ ") +
                 f"{job_id} （ch={channel_id} / {provider_label} / 並列 {concurrency} / style={style_preset} / route={route_mode} / Web画像 {web_image_count}）")

        def on_progress(phase, msg, pct):
            _set_job_state(job_id, status="running", phase=phase, message=msg, percent=pct)

        def on_log(category, message, detail=""):
            _add_log(job_id, category, message, detail)

        def on_item(info):
            pass  # rows_progress.json 経由でフロントへ

        defaults = get_channel(channel_id).get("defaults") or {}
        pipeline = SentencePipeline(
            manuscript_text=manuscript_text,
            output_dir=job_dir,
            user_instructions=user_instructions,
            concurrency=concurrency,
            provider=provider,
            openai_quality=openai_quality,
            style_preset=style_preset,
            worldview_desc=worldview_desc,
            verify_diagrams=verify_diagrams,
            channel_id=channel_id,
            anthropic_key=ch_keys.get("anthropic", ""),
            gemini_key=ch_keys.get("gemini", ""),
            openai_key=ch_keys.get("openai", ""),
            character_ref_path=character_ref_path,
            skip_decorative=skip_decorative,
            web_image_count=web_image_count,
            max_diagrams=max_diagrams,
            route_mode=route_mode,
            chart_engine=defaults.get("chart_engine", "ai"),
            allow_charts=defaults.get("allow_charts", True),
            map_engine=defaults.get("map_engine", "ai"),
            allow_maps=defaults.get("allow_maps", False),
            intro_visual_boost=defaults.get("intro_visual_boost", 0),
            map_route_limit=defaults.get("map_route_limit", 0),
            realistic_route_min=defaults.get("realistic_route_min", 0),
            no_image_text=defaults.get("no_image_text", False),
            photo_source=defaults.get("photo_source", "web"),
            web_search_profile=defaults.get("web_search_profile", ""),
            max_web_image_reuse=defaults.get("max_web_image_reuse", 2),
            type_providers=defaults.get("type_providers", {}),
            beat_mode=bool(defaults.get("beat_mode", False)),
            chars_per_sec=defaults.get("chars_per_sec", 5.5),
            realphoto_watermark=bool(defaults.get("realphoto_watermark", False)),
            chart_theme=defaults.get("chart_theme"),
            generation_batch_size=defaults.get("generation_batch_size", 0),
            generation_batch_mode=defaults.get("generation_batch_mode", "block"),
            router_concurrency=defaults.get("router_concurrency", 2),
            title_override=title_override,
            fact_context=fact_context,
            resume=resume,
            progress_callback=on_progress,
            log_callback=on_log,
            item_callback=on_item,
        )
        manifest = pipeline.run()
        _set_job_state(
            job_id,
            status="completed",
            phase=4,
            message=f"完了: 生成 {manifest['generated']} / 全 {manifest['total_sentences']} 文",
            percent=100,
            title=manifest.get("title", ""),
            generated=manifest.get("generated", 0),
            failed=manifest.get("failed", 0),
            total_sentences=manifest.get("total_sentences", 0),
        )
        _add_log(job_id, "system",
                 f"全フェーズ完了（成功 {manifest['generated']} / 失敗 {manifest['failed']}）")
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            rows_path = job_dir / "rows_progress.json"
            snap = load_json(rows_path, {"rows": []})
            dirty = False
            for r in snap.get("rows", []):
                if r.get("status") in ("generating", "pending") and r.get("display") == "image":
                    r["status"] = "failed"
                    r["error"] = f"ジョブ中断: {str(e)[:120]}"
                    dirty = True
            if dirty:
                rows_path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        _set_job_state(job_id, status="error", message=str(e)[:200], percent=0)
        _add_log(job_id, "error", "パイプライン実行エラー", str(e)[:300])


# ====== ルート ======
@app.route("/")
@login_required
def index():
    past_jobs = []
    if OUTPUT_DIR.exists():
        for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
            if len(past_jobs) >= 30:
                break
            if not d.is_dir():
                continue
            manifest = load_json(d / "manifest.json", {})
            job_state = load_json(d / "job.json", {})
            if not manifest and not job_state:
                continue
            # シーン直しつくーるの出力は一括生成用の /progress では開けない。
            # 最近のジョブには通常のセンテンス生成ジョブだけを表示する。
            if manifest.get("tool") == "scene_fix" or d.name.startswith("scene_fix_"):
                continue
            ch_id = manifest.get("channel_id", job_state.get("channel_id", ""))
            past_jobs.append({
                "id": d.name,
                "title": manifest.get("title", job_state.get("title", d.name)),
                "status": job_state.get("status", "unknown"),
                "generated": manifest.get("generated", job_state.get("generated", 0)),
                "total": manifest.get("total_sentences", job_state.get("total_sentences", 0)),
                "channel": ch_id,
                "date": d.name[:8] if len(d.name) >= 8 else "",
                # 未完了（manifest 無し）かつ原稿が残っていれば途中から再開できる
                "resumable": (not manifest) and (d / "manuscript.txt").exists(),
            })
    # 各チャンネルのキー設定状況（UI 表示用）
    channels = load_channels()
    for c in channels:
        keys = resolve_channel_keys(c)
        c["_has_gemini"] = bool(keys["gemini"])
        c["_has_openai"] = bool(keys["openai"])
        c["_has_anthropic"] = bool(keys["anthropic"])
    return render_template(
        "upload.html",
        past_jobs=past_jobs[:30],
        channels=channels,
        has_anthropic=bool(os.environ.get("ANTHROPIC_API_KEY")),
        has_gemini=bool(os.environ.get("GEMINI_API_KEY")),
        has_openai=bool(os.environ.get("OPENAI_API_KEY")),
    )


def _scene_fix_variant_hint(route: str, variant_no: int) -> str:
    diagram_hints = [
        "Variant A: causal flow diagram, left-to-right, cause -> mechanism -> result.",
        "Variant B: relationship map, center-out, main subject in the center with 3 connected nodes.",
        "Variant C: comparison diagram, two balanced sides with a small conclusion.",
        "Variant D: process diagram, three steps connected by arrows.",
        "Variant E: minimal bold infographic, few large shapes and strong reading path.",
    ]
    realphoto_hints = [
        "Variant A: wide documentary news still, establishes the real-world scene.",
        "Variant B: medium shot with people, workplace, meeting, street, or institution context.",
        "Variant C: close-up of a relevant object, document, facility, product, or symbolic real detail.",
        "Variant D: cinematic editorial photograph with strong composition and natural lighting.",
        "Variant E: neutral documentary scene with more empty space for editing.",
    ]
    illustration_hints = [
        "Variant A: symbolic flat illustration with clear foreground subject.",
        "Variant B: infographic-style illustration with arrows and simple icons.",
        "Variant C: scene-based educational illustration with a human-scale context.",
        "Variant D: minimal icon composition with strong negative space.",
        "Variant E: editorial explainer illustration, calm and serious tone.",
    ]
    table = {
        "diagram": diagram_hints,
        "realphoto": realphoto_hints,
        "illustration": illustration_hints,
    }.get(route, diagram_hints)
    return table[(variant_no - 1) % len(table)]


def _scene_fix_mode_instruction(mode: str) -> str:
    table = {
        "balanced": "Create distinct options with different composition choices while keeping the same factual meaning.",
        "more_clear": "Prioritize clarity over decoration. Use a simple structure, strong hierarchy, and an obvious reading order.",
        "less_text": "Reduce text volume. Use only the few most important words from the source sentence and let the composition explain the idea.",
        "more_real": "Make the scene more realistic and usable as video material. Prefer concrete setting, natural light, and editorial restraint.",
        "same_style": "Use the attached reference image as the style/composition anchor. Keep the original direction and make controlled improvements only.",
    }
    return table.get(mode, table["balanced"])


def _scene_fix_allowed_terms(sentence: str, route: str) -> list:
    """シーン直し用の画像内テキスト候補。原稿にある語だけを許可する。"""
    try:
        from prompter import _auto_extract_terms, _limit_allowed_terms
        return _limit_allowed_terms(
            _auto_extract_terms(sentence),
            sentence,
            allow_connectors=False,
        )
    except Exception:
        return []


def _build_scene_fix_prompt(sentence: str, route: str, variant_no: int, extra: str, defaults: dict, fix_mode: str = "balanced") -> str:
    """1シーン修正用の複数案プロンプト。制約は軽く、でも事実は増やさない。"""
    hint = _scene_fix_variant_hint(route, variant_no)
    worldview = (defaults.get("worldview_desc") or "").strip()
    user_instructions = (defaults.get("user_instructions") or "").strip()
    route_rule = {
        "diagram": (
            "Create a clear educational diagram. The priority is comprehension: one visual goal, "
            "a clear reading path, 3-5 connected elements, arrows/lines that explain relationships, "
            "not isolated keyword cards."
        ),
        "realphoto": (
            "Create a photorealistic documentary image. No Japanese labels, no infographic UI, "
            "no flat illustration. It should look like a usable editorial/video material still."
        ),
        "illustration": (
            "Create a serious educational illustration. It can use symbols and simple arrows, "
            "but should not become cute, childish, or decorative."
        ),
    }.get(route, "Create a clear educational image.")
    parts = [
        route_rule,
        hint,
        "Fix mode: " + _scene_fix_mode_instruction(fix_mode),
        f"Source sentence: {sentence}",
        "Do not add new factual claims, names, countries, dates, numbers, or entities that are not supported by the sentence.",
        "Make it suitable as one scene in an educational video.",
    ]
    if route == "diagram":
        parts.append(
            "For labels: use only short factual labels that appear in the source sentence. Do not display generic structural labels such as 原因, 結果, 背景, 影響, 依存, 対立, 比較, 流れ unless that exact word appears in the sentence and is explicitly allowed. Express structure with arrows, grouping, placement, and line direction instead."
        )
    if user_instructions:
        parts.append("Channel quality instructions: " + user_instructions[:1000])
    if worldview and route in ("diagram", "illustration"):
        parts.append("Visual world / tone: " + worldview[:1000])
    if extra:
        parts.append("Editor request: " + extra[:800])
    return "\n".join(parts)


def _save_scene_fix_reference(job_dir: Path):
    f = request.files.get("reference_image")
    if not f or not f.filename:
        return None
    suffix = Path(f.filename).suffix.lower()
    if suffix not in (".png", ".jpg", ".jpeg", ".webp"):
        return None
    ref_dir = job_dir / "reference"
    ref_dir.mkdir(parents=True, exist_ok=True)
    path = ref_dir / f"source{suffix}"
    f.save(path)
    return path


@app.route("/scene-fix")
@login_required
def scene_fix_page():
    channels = load_channels()
    for c in channels:
        keys = resolve_channel_keys(c)
        c["_has_gemini"] = bool(keys["gemini"])
        c["_has_openai"] = bool(keys["openai"])
        c["_has_anthropic"] = bool(keys["anthropic"])
    return render_template(
        "scene_fix.html",
        channels=channels,
        providers=VALID_PROVIDERS,
    )


@app.route("/api/scene-fix", methods=["POST"])
@login_required
def api_scene_fix():
    """1センテンスから4〜5案を生成する個別修正ツール。"""
    from generator import run_parallel_generation, PROVIDER_NANOBANANA

    sentence = (request.form.get("sentence") or "").strip()
    if len(sentence) < 3:
        return jsonify({"ok": False, "error": "センテンスを入力してください"}), 400
    route = (request.form.get("route") or "diagram").strip()
    if route not in ("diagram", "realphoto", "illustration"):
        return jsonify({"ok": False, "error": "画像タイプが不正です"}), 400
    try:
        variant_count = int(request.form.get("variant_count") or 4)
    except ValueError:
        variant_count = 4
    variant_count = max(1, min(5, variant_count))

    channel_id = request.form.get("channel_id", "default")
    channel = get_channel(channel_id)
    channel_id = channel.get("id", "default")
    ch_keys = resolve_channel_keys(channel)
    defaults = channel.get("defaults", {}) or {}
    style_preset = defaults.get("style_preset", "flat_infographic")
    if style_preset not in VALID_STYLES:
        style_preset = "flat_infographic"

    provider = (request.form.get("provider") or "auto").strip()
    if provider == "auto":
        type_providers = defaults.get("type_providers") or {}
        provider = type_providers.get(route) or defaults.get("provider") or PROVIDER_NANOBANANA
    if provider not in VALID_PROVIDERS:
        provider = PROVIDER_NANOBANANA
    openai_quality = (request.form.get("openai_quality") or defaults.get("openai_quality") or "medium").strip()
    if openai_quality not in ("low", "medium", "high"):
        openai_quality = "medium"
    fix_mode = (request.form.get("fix_mode") or "balanced").strip()
    if fix_mode not in ("balanced", "more_clear", "less_text", "more_real", "same_style"):
        fix_mode = "balanced"
    extra = (request.form.get("extra_instruction") or "").strip()

    job_id = f"scene_fix_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"
    job_dir = OUTPUT_DIR / job_id
    images_dir = job_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    reference_image_path = _save_scene_fix_reference(job_dir)

    allowed_terms = _scene_fix_allowed_terms(sentence, route)
    prompts = []
    for i in range(1, variant_count + 1):
        prompts.append({
            "index": i,
            "prompt": _build_scene_fix_prompt(sentence, route, i, extra, defaults, fix_mode=fix_mode),
            "type": route,
            "section": "シーン直し",
            "excerpt": sentence,
            "keypoint": sentence[:30],
            "allowed_terms": allowed_terms if route in ("diagram", "illustration") else [],
            "style": style_preset,
            "character": False,
            "edit_source": bool(reference_image_path),
        })

    events = []
    def on_progress(ev):
        events.append(ev)

    try:
        results = run_parallel_generation(
            prompts=prompts,
            output_dir=images_dir,
            provider=provider,
            gemini_api_key=ch_keys.get("gemini") or None,
            openai_api_key=ch_keys.get("openai") or None,
            openai_quality=openai_quality,
            concurrency=min(variant_count, 3),
            style_preset=style_preset,
            progress_callback=on_progress,
            realphoto_watermark=bool(defaults.get("realphoto_watermark", False)) and route == "realphoto",
            edit_image_path=str(reference_image_path) if reference_image_path else None,
        )
        # 参照画像つきの編集生成は provider 側の制約で全滅することがある。
        # 生成ボタン自体は止めず、通常生成へ自動フォールバックする。
        if reference_image_path and not any(r.get("success") for r in results):
            events.append({
                "status": "fallback",
                "message": "参照画像つき生成が失敗したため、通常生成に切り替えました",
            })
            fallback_prompts = [{**p, "edit_source": False} for p in prompts]
            results = run_parallel_generation(
                prompts=fallback_prompts,
                output_dir=images_dir,
                provider=provider,
                gemini_api_key=ch_keys.get("gemini") or None,
                openai_api_key=ch_keys.get("openai") or None,
                openai_quality=openai_quality,
                concurrency=min(variant_count, 3),
                style_preset=style_preset,
                progress_callback=on_progress,
                realphoto_watermark=bool(defaults.get("realphoto_watermark", False)) and route == "realphoto",
            )
    except Exception as e:
        return jsonify({"ok": False, "error": f"生成に失敗しました: {str(e)[:180]}"}), 500

    variants = []
    for r in results:
        filename = r.get("filename") if r.get("success") else ""
        variants.append({
            "index": r.get("index"),
            "ok": bool(r.get("success")),
            "filename": filename,
            "url": f"/results/{job_id}/images/{filename}" if filename else "",
            "error": r.get("error", ""),
            "provider": r.get("provider", provider),
            "variant_hint": _scene_fix_variant_hint(route, int(r.get("index") or 1)),
        })

    manifest = {
        "tool": "scene_fix",
        "job_id": job_id,
        "channel_id": channel_id,
        "route": route,
        "provider": provider,
        "style_preset": style_preset,
        "fix_mode": fix_mode,
        "reference_image": f"reference/{reference_image_path.name}" if reference_image_path else "",
        "sentence": sentence,
        "extra_instruction": extra,
        "allowed_terms": allowed_terms,
        "variants": variants,
        "events": events[-30:],
        "created_at": datetime.now().isoformat(),
    }
    (job_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (job_dir / "scene.txt").write_text(sentence, encoding="utf-8")

    return jsonify({"ok": True, **manifest})


@app.route("/api/scene-fix/<job_id>/revise", methods=["POST"])
@login_required
def api_scene_fix_revise(job_id):
    """生成済みの案を元画像として、追加指示でさらに1枚だけ修正する。"""
    from generator import run_parallel_generation, PROVIDER_NANOBANANA

    job_dir = _safe_job_dir(job_id)
    if not job_dir or not job_dir.exists():
        return jsonify({"ok": False, "error": "ジョブが見つかりません"}), 404
    manifest_path = job_dir / "manifest.json"
    manifest = load_json(manifest_path, {})
    try:
        source_index = int(request.form.get("index") or 0)
    except ValueError:
        source_index = 0
    instruction = (request.form.get("instruction") or "").strip()
    if len(instruction) < 2:
        return jsonify({"ok": False, "error": "修正指示を入力してください"}), 400

    variants = manifest.get("variants", [])
    variant = next((v for v in variants if int(v.get("index") or 0) == source_index), None)
    if not variant or not variant.get("ok") or not variant.get("filename"):
        return jsonify({"ok": False, "error": "修正元の画像が見つかりません"}), 400

    images_dir = job_dir / "images"
    source_image = (images_dir / Path(str(variant.get("filename"))).name).resolve()
    try:
        source_image.relative_to(images_dir.resolve())
    except ValueError:
        return jsonify({"ok": False, "error": "修正元の画像パスが不正です"}), 400
    if not source_image.exists():
        return jsonify({"ok": False, "error": "修正元の画像ファイルがありません"}), 404

    channel_id = manifest.get("channel_id", "default")
    channel = get_channel(channel_id)
    ch_keys = resolve_channel_keys(channel)
    defaults = channel.get("defaults", {}) or {}
    route = manifest.get("route", "diagram")
    if route not in ("diagram", "realphoto", "illustration"):
        route = "diagram"
    provider = manifest.get("provider") or PROVIDER_NANOBANANA
    if provider not in VALID_PROVIDERS:
        provider = PROVIDER_NANOBANANA
    style_preset = manifest.get("style_preset") or defaults.get("style_preset", "flat_infographic")
    if style_preset not in VALID_STYLES:
        style_preset = "flat_infographic"
    openai_quality = defaults.get("openai_quality", "medium")
    if openai_quality not in ("low", "medium", "high"):
        openai_quality = "medium"

    revisions = manifest.get("revisions", [])
    revision_no = 1 + sum(1 for r in revisions if int(r.get("source_index") or 0) == source_index)
    revised_index = f"{source_index}_rev{revision_no}"
    sentence = manifest.get("sentence", "")
    base_extra = manifest.get("extra_instruction", "")
    extra = (base_extra + "\n" + "Further editor revision: " + instruction).strip()
    prompt = _build_scene_fix_prompt(
        sentence,
        route,
        source_index,
        extra,
        defaults,
        fix_mode="same_style",
    )
    prompt = (
        "Refine the attached generated image. Keep the useful parts of the current composition, "
        "but apply the editor revision exactly. Do not introduce unsupported facts.\n"
        + prompt
    )

    entry = {
        "index": revised_index,
        "prompt": prompt,
        "type": route,
        "section": "シーン直し 再修正",
        "excerpt": sentence,
        "keypoint": sentence[:30],
        "allowed_terms": manifest.get("allowed_terms", []) if route in ("diagram", "illustration") else [],
        "style": style_preset,
        "character": False,
        "edit_source": True,
    }

    try:
        results = run_parallel_generation(
            prompts=[entry],
            output_dir=images_dir,
            provider=provider,
            gemini_api_key=ch_keys.get("gemini") or None,
            openai_api_key=ch_keys.get("openai") or None,
            openai_quality=openai_quality,
            concurrency=1,
            style_preset=style_preset,
            edit_image_path=str(source_image),
            realphoto_watermark=bool(defaults.get("realphoto_watermark", False)) and route == "realphoto",
        )
    except Exception as e:
        return jsonify({"ok": False, "error": f"再修正に失敗しました: {str(e)[:180]}"}), 500

    r = results[0] if results else {}
    if not r.get("success") or not r.get("filename"):
        return jsonify({"ok": False, "error": r.get("error", "再修正に失敗しました")}), 500

    revised = {
        "source_index": source_index,
        "revision_no": revision_no,
        "index": revised_index,
        "filename": r.get("filename"),
        "url": f"/results/{job_id}/images/{r.get('filename')}",
        "instruction": instruction,
        "provider": r.get("provider", provider),
        "created_at": datetime.now().isoformat(),
    }
    revisions.append(revised)
    manifest["revisions"] = revisions
    variant["previous_filename"] = variant.get("filename", "")
    variant["filename"] = revised["filename"]
    variant["url"] = revised["url"]
    variant["revision_no"] = revision_no
    variant["revised_at"] = revised["created_at"]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return jsonify({"ok": True, "job_id": job_id, "variant": variant, "revision": revised})


@app.route("/api/scene-fix/<job_id>/select", methods=["POST"])
@login_required
def api_scene_fix_select(job_id):
    """シーン直しの採用案を軽量に記録する。"""
    job_dir = _safe_job_dir(job_id)
    if not job_dir or not job_dir.exists():
        return jsonify({"ok": False, "error": "ジョブが見つかりません"}), 404
    manifest = load_json(job_dir / "manifest.json", {})
    try:
        index = int(request.form.get("index") or 0)
    except ValueError:
        index = 0
    variant = next((v for v in manifest.get("variants", []) if int(v.get("index") or 0) == index), None)
    if not variant or not variant.get("ok"):
        return jsonify({"ok": False, "error": "採用できる案が見つかりません"}), 400
    selected = {
        "job_id": job_id,
        "selected_index": index,
        "filename": variant.get("filename", ""),
        "url": variant.get("url", ""),
        "selected_at": datetime.now().isoformat(),
    }
    (job_dir / "selected_variant.json").write_text(json.dumps(selected, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest["selected_variant"] = selected
    (job_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"ok": True, **selected})


@app.route("/download/scene-fix/<job_id>")
@login_required
def download_scene_fix_zip(job_id):
    """シーン直しの全案をZIPでダウンロード。"""
    job_dir = _safe_job_dir(job_id)
    if not job_dir or not job_dir.exists():
        return "結果が見つかりません", 404

    import tempfile
    tmp = tempfile.NamedTemporaryFile(prefix=f"{job_id}_", suffix=".zip", delete=False)
    tmp_path = tmp.name
    tmp.close()

    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:
        for rel in ("manifest.json", "selected_variant.json", "scene.txt"):
            p2 = job_dir / rel
            if p2.exists():
                zf.write(p2, rel)
        images_dir = job_dir / "images"
        if images_dir.exists():
            for img in sorted(images_dir.iterdir()):
                if img.is_file() and img.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                    zf.write(img, f"images/{img.name}")
        ref_dir = job_dir / "reference"
        if ref_dir.exists():
            for img in sorted(ref_dir.iterdir()):
                if img.is_file():
                    zf.write(img, f"reference/{img.name}")
        zf.writestr("README.txt", "シーン直しつくーるの生成案一式です。selected_variant.json がある場合は採用案を示します。\n")

    return _send_temp_zip(tmp_path, f"{job_id}_scene_fix.zip")


@app.route("/start", methods=["POST"])
@login_required
def start_job():
    # チャンネル選択 → そのチャンネルの API キーを解決
    channel_id = request.form.get("channel_id", "default")
    channel = get_channel(channel_id)
    channel_id = channel.get("id", "default")
    ch_keys = resolve_channel_keys(channel)

    provider = request.form.get("provider", PROVIDER_NANOBANANA)
    if provider not in VALID_PROVIDERS:
        provider = PROVIDER_NANOBANANA
    openai_quality = request.form.get("openai_quality", "medium")
    if openai_quality not in ("low", "medium", "high"):
        openai_quality = "medium"
    skip_decorative = request.form.get("skip_decorative", "off") == "on"
    style_preset = (channel.get("defaults", {}) or {}).get("style_preset", "flat_infographic")
    if style_preset not in VALID_STYLES:
        style_preset = "flat_infographic"
    route_mode = request.form.get("route_mode", "auto")
    if route_mode not in ("auto", "all_ai"):
        route_mode = "auto"
    # 世界観統一モード（チェックON時のみ description を有効化）
    worldview_on = request.form.get("worldview_mode", "off") == "on"
    worldview_desc = request.form.get("worldview_desc", "").strip() if worldview_on else ""
    # ON なのに本文が空（フォーム未入力など）なら、チャンネル既定の世界観へフォールバック。
    # これで「先生キャラ等の設定が空欄で効かない」事故を防ぐ。
    if worldview_on and not worldview_desc:
        worldview_desc = (channel.get("defaults", {}) or {}).get("worldview_desc", "").strip()
    # キャラ固定の参照画像（チャンネル設定 character_ref）。存在すれば絶対パスを渡す。
    # 世界観モードON のときだけ有効（キャラ統一の一部）。
    character_ref_path = ""
    if worldview_on:
        _cref = (channel.get("defaults", {}) or {}).get("character_ref", "").strip()
        if _cref:
            _crp = PROJECT_ROOT / _cref
            if _crp.exists():
                character_ref_path = str(_crp)
    verify_diagrams = request.form.get("verify_diagrams", "off") == "on"
    try:
        web_image_count = int(request.form.get("web_image_count", "0"))
    except ValueError:
        web_image_count = 0
    web_image_count = max(0, min(web_image_count, 200))
    try:
        max_diagrams = int(request.form.get("max_diagrams", "150"))
    except ValueError:
        max_diagrams = 150
    max_diagrams = max(1, min(max_diagrams, 300))

    # API キー確認（チャンネルのキー＝個別 or 共通フォールバック）
    missing = []
    if not ch_keys["anthropic"]:
        missing.append("ANTHROPIC_API_KEY")
    defaults = channel.get("defaults", {}) or {}
    required_providers = {provider}
    for p in (defaults.get("type_providers", {}) or {}).values():
        if p in VALID_PROVIDERS:
            required_providers.add(p)
    if PROVIDER_NANOBANANA in required_providers and not ch_keys["gemini"]:
        missing.append("GEMINI_API_KEY")
    if PROVIDER_GPT_IMAGE in required_providers and not ch_keys["openai"]:
        missing.append("OPENAI_API_KEY")
    if missing:
        pfx = channel.get("api_env_prefix", "")
        hint = f"（チャンネル「{channel.get('name','')}」用に {pfx}_... を設定するか共通キーを設定）" if pfx else ""
        return jsonify({"error": f"{', '.join(missing)} が設定されていません{hint}"}), 400

    # 原稿取得（.docx は見出しを章として解析 / .json または貼り付けJSONは原稿パイプライン final.json 直結）
    from utils import parse_final_json, extract_from_final_json
    manuscript_text = ""
    prebuilt_chapters = None
    title_override = ""      # v3 Step7: final.json の tentative_title をタイトルに
    fact_context = ""        # v3 Step7: final.json の fact_report 等を chart 抽出の文脈に
    _final_obj = None
    if "manuscript_file" in request.files and request.files["manuscript_file"].filename:
        f = request.files["manuscript_file"]
        fname = f.filename.lower()
        raw = f.read()
        if fname.endswith(".docx"):
            try:
                from splitter import parse_docx_to_chapters
                manuscript_text, prebuilt_chapters = parse_docx_to_chapters(raw)
                if not prebuilt_chapters:
                    return jsonify({"error": ".docx から本文を抽出できませんでした"}), 400
            except Exception as e:
                return jsonify({"error": f".docx の解析に失敗: {str(e)[:120]}"}), 400
        else:
            decoded = raw.decode("utf-8", errors="ignore")
            _final_obj = parse_final_json(decoded)
            if fname.endswith(".json") and _final_obj is None:
                return jsonify({"error": "final.json の形式が不正です（本文の final キーが見つかりません）"}), 400
            if _final_obj is None:
                manuscript_text = decoded
    elif request.form.get("manuscript_text"):
        pasted = request.form["manuscript_text"]
        _final_obj = parse_final_json(pasted)  # JSON を貼り付けても final.json として扱う
        if _final_obj is None:
            manuscript_text = pasted
    else:
        return jsonify({"error": "原稿が入力されていません"}), 400

    # final.json から本文・タイトル・検証文脈を取り出す（存在しないキーは任意扱い）
    if _final_obj is not None:
        _info = extract_from_final_json(_final_obj)
        manuscript_text = _info["manuscript"]
        title_override = _info["title"]
        fact_context = _info["fact_context"]

    if len(manuscript_text.strip()) < 100:
        return jsonify({"error": "原稿が短すぎます（100文字以上必要）"}), 400

    try:
        concurrency = int(request.form.get("concurrency", "4"))
    except ValueError:
        concurrency = 4
    concurrency = max(1, min(concurrency, 8))

    user_instructions = request.form.get("user_instructions", "").strip()
    if not user_instructions:
        user_instructions = (channel.get("defaults", {}) or {}).get("user_instructions", "").strip()

    job_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "manuscript.txt").write_text(manuscript_text, encoding="utf-8")
    if user_instructions:
        (job_dir / "user_instructions.txt").write_text(user_instructions, encoding="utf-8")
    # .docx の見出しから作った章構造を保存（pipeline が読み込む）
    if prebuilt_chapters:
        (job_dir / "prebuilt_chapters.json").write_text(
            json.dumps({"chapters": prebuilt_chapters}, ensure_ascii=False), encoding="utf-8")

    # 冒頭・終わりの固定画像（任意）を保存（images/intro.*, images/outro.*）
    (job_dir / "images").mkdir(parents=True, exist_ok=True)
    for slot in ("intro", "outro"):
        f = request.files.get(f"{slot}_image")
        if f and f.filename:
            ext = os.path.splitext(f.filename)[1].lower()
            if ext in (".png", ".jpg", ".jpeg", ".webp"):
                f.save(str(job_dir / "images" / f"{slot}{ext}"))

    _set_job_state(
        job_id,
        status="queued",
        phase=0,
        message="キューに追加しました",
        percent=0,
        channel_id=channel_id,
        concurrency=concurrency,
        provider=provider,
        openai_quality=openai_quality if provider == PROVIDER_GPT_IMAGE else None,
        skip_decorative=skip_decorative,
        style_preset=style_preset,
        web_image_count=web_image_count,
        max_diagrams=max_diagrams,
        # 途中再開用に全パラメータを保存（サーバー再起動でメモリが消えても復元できる）
        route_mode=route_mode,
        worldview_desc=worldview_desc,
        verify_diagrams=verify_diagrams,
        title_override=title_override,
        fact_context=fact_context,
    )

    thread = threading.Thread(
        target=_run_pipeline_thread,
        args=(job_id, manuscript_text, user_instructions, concurrency, provider, openai_quality,
              skip_decorative, style_preset, web_image_count, max_diagrams, route_mode, worldview_desc, verify_diagrams,
              channel_id, ch_keys, character_ref_path, title_override, fact_context),
        daemon=True,
    )
    thread.start()
    return jsonify({"job_id": job_id, "redirect": f"/progress/{job_id}"})


def _build_resume_args(job_id: str):
    """途中で止まったジョブの再開引数を、ディスク上の保存情報から復元する。

    戻り値: (args_tuple, error_message)。error_message が None なら再開可能。
    旧ジョブ（パラメータ未保存）はチャンネル既定値で補完する。
    """
    job_dir = _safe_job_dir(job_id)
    if not job_dir or not job_dir.exists():
        return None, "ジョブのデータがサーバー上にありません（再デプロイ等で消えた可能性）"
    if (job_dir / "manifest.json").exists():
        return None, "このジョブは完了済みです（再開は不要）"
    manuscript_path = job_dir / "manuscript.txt"
    if not manuscript_path.exists():
        return None, "原稿ファイルが見つからないため再開できません（新規ジョブとして作り直してください）"
    try:
        manuscript_text = manuscript_path.read_text(encoding="utf-8")
    except Exception:
        return None, "原稿ファイルを読み込めませんでした"
    if len(manuscript_text.strip()) < 100:
        return None, "保存された原稿が短すぎるため再開できません"

    job_state = load_json(job_dir / "job.json", {})
    channel = get_channel(job_state.get("channel_id", "default"))
    channel_id = channel.get("id", "default")
    defaults = channel.get("defaults", {}) or {}
    ch_keys = resolve_channel_keys(channel)

    user_instructions = ""
    ui_path = job_dir / "user_instructions.txt"
    if ui_path.exists():
        try:
            user_instructions = ui_path.read_text(encoding="utf-8")
        except Exception:
            pass

    def _int_of(key, fallback, lo, hi):
        try:
            v = int(job_state.get(key) if job_state.get(key) is not None else fallback)
        except (TypeError, ValueError):
            v = int(fallback)
        return max(lo, min(hi, v))

    provider = job_state.get("provider") or defaults.get("provider", PROVIDER_NANOBANANA)
    if provider not in VALID_PROVIDERS:
        provider = PROVIDER_NANOBANANA
    openai_quality = job_state.get("openai_quality") or defaults.get("openai_quality", "medium")
    if openai_quality not in ("low", "medium", "high"):
        openai_quality = "medium"
    style_preset = job_state.get("style_preset") or defaults.get("style_preset", "flat_infographic")
    if style_preset not in VALID_STYLES:
        style_preset = "flat_infographic"
    route_mode = job_state.get("route_mode") or defaults.get("route_mode", "auto")
    if route_mode not in ("auto", "all_ai"):
        route_mode = "auto"
    worldview_desc = job_state.get("worldview_desc")
    if worldview_desc is None:
        # 旧ジョブ: worldview 指定が保存されていない → チャンネル既定にフォールバック
        worldview_desc = defaults.get("worldview_desc", "") if defaults.get("worldview_mode", True) else ""
    verify_diagrams = job_state.get("verify_diagrams")
    if verify_diagrams is None:
        verify_diagrams = bool(defaults.get("verify_diagrams", False))
    skip_decorative = bool(job_state.get("skip_decorative", defaults.get("skip_decorative", False)))
    concurrency = _int_of("concurrency", defaults.get("concurrency", 4), 1, 24)
    web_image_count = _int_of("web_image_count", defaults.get("web_image_count", 0), 0, 200)
    max_diagrams = _int_of("max_diagrams", defaults.get("max_diagrams", 150), 1, 300)
    title_override = job_state.get("title_override") or ""
    fact_context = job_state.get("fact_context") or ""

    character_ref_path = ""
    _cref = (defaults.get("character_ref") or "").strip()
    if worldview_desc and _cref:
        _crp = PROJECT_ROOT / _cref
        if _crp.exists():
            character_ref_path = str(_crp)

    args = (job_id, manuscript_text, user_instructions, concurrency, provider, openai_quality,
            skip_decorative, style_preset, web_image_count, max_diagrams, route_mode,
            worldview_desc, bool(verify_diagrams), channel_id, ch_keys, character_ref_path,
            title_override, fact_context, True)  # resume=True
    return args, None


@app.route("/api/resume/<job_id>", methods=["POST"])
@login_required
def api_resume(job_id):
    """途中で止まったジョブを再開する（完了済みの成果物はスキップして残りだけ実行）。"""
    with _jobs_lock:
        st = (_jobs.get(job_id) or {}).get("status")
    if st == "running":
        return jsonify({"error": "このジョブは現在実行中です"}), 409
    args, err = _build_resume_args(job_id)
    if err:
        return jsonify({"error": err}), 400
    _set_job_state(job_id, status="queued", phase=0, message="再開の準備中...", percent=0)
    _add_log(job_id, "system", "再開リクエストを受け付けました（完了済みの成果物は再利用します）")
    threading.Thread(target=_run_pipeline_thread, args=args, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id, "redirect": f"/progress/{job_id}"})


@app.route("/progress/<job_id>")
@login_required
def progress_page(job_id):
    return render_template("progress.html", job_id=job_id)


@app.route("/api/status/<job_id>")
@login_required
def api_status(job_id):
    state = _get_job_state(job_id)
    if not state:
        return jsonify({"status": "not_found"}), 404
    return jsonify(state)


@app.route("/api/rows/<job_id>")
@login_required
def api_rows(job_id):
    """センテンス行の進捗"""
    snapshot = load_json(OUTPUT_DIR / job_id / "rows_progress.json", {"rows": []})
    return jsonify(snapshot)


@app.route("/api/logs/<job_id>")
@login_required
def api_logs(job_id):
    since = int(request.args.get("since", 0))
    with _jobs_lock:
        logs = list(_job_logs.get(job_id, []))
    if not logs:
        logs = load_json(OUTPUT_DIR / job_id / "logs.json", [])
    return jsonify({"logs": logs[since:], "total": len(logs)})


@app.route("/api/manifest/<job_id>")
@login_required
def api_manifest(job_id):
    manifest = load_json(OUTPUT_DIR / job_id / "manifest.json", {})
    return jsonify(manifest)


@app.route("/api/jobs/<job_id>", methods=["DELETE", "POST"])
@login_required
def api_delete_job(job_id):
    """過去ジョブの出力一式を削除する。"""
    job_dir = _safe_job_dir(job_id)
    if not job_dir or not job_dir.exists() or not job_dir.is_dir():
        return jsonify({"ok": False, "error": "ジョブが見つかりません"}), 404

    try:
        shutil.rmtree(job_dir)
    except Exception as e:
        return jsonify({"ok": False, "error": f"削除に失敗しました: {str(e)[:150]}"}), 500

    with _jobs_lock:
        _jobs.pop(job_id, None)
        _job_logs.pop(job_id, None)
    return jsonify({"ok": True, "job_id": job_id})


def _update_regen_snapshot(job_dir, no, ok, filename=None, engine=None, route=None, route_reason=None, status=None, extra=None):
    """再生成結果を rows_progress.json に反映（chart/map/AI 共通）。

    read-modify-write 全体を SNAPSHOT_IO_LOCK で直列化する。
    同時に2枚再生成した場合や、実行中ジョブの _dump_snapshot と重なった場合に
    片方の更新が失われる競合を防ぐ。
    """
    import json as _json
    snap_path = job_dir / "rows_progress.json"
    with SNAPSHOT_IO_LOCK:
        snap = load_json(snap_path, {"rows": []})
        for r in snap.get("rows", []):
            if r.get("no") == no:
                r["status"] = status or ("ok" if ok else "failed")
                if filename:
                    r["filename"] = filename
                if engine:
                    r["engine"] = engine
                if route:
                    r["route"] = route
                if route_reason:
                    r["route_reason"] = route_reason
                if extra:
                    r.update(extra)
                break
        try:
            snap_path.write_text(_json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


def _mark_regen_failed(job_dir, no, message, route=None, route_reason=None):
    """再生成失敗を snapshot に残し、UI が生成中のまま固まらないようにする。"""
    _update_regen_snapshot(
        job_dir,
        no,
        False,
        route=route or None,
        route_reason=route_reason or "再生成失敗",
        status="failed",
        extra={
            "error": str(message or "再生成失敗")[:300],
            "regen_finished_at": datetime.now().isoformat(timespec="seconds"),
            "verify_issue": True,
        },
    )


def _find_existing_scene_image(job_dir, no, snap_row=None, target=None):
    """個別再生成の元画像を探す。見つからなければ None（従来の新規生成へ）。"""
    image_dir = job_dir / "images"
    candidates = []
    for row in (snap_row or {}, target or {}):
        fname = row.get("filename") or row.get("web_local_file")
        if fname:
            candidates.append(image_dir / Path(str(fname)).name)
    candidates.extend([
        image_dir / f"{no}.png",
        image_dir / f"{no}.jpg",
        image_dir / f"{no}.jpeg",
        image_dir / f"{no}.webp",
    ])
    for p in candidates:
        try:
            if p.exists() and p.stat().st_size > 50:
                return p
        except Exception:
            continue
    return None


def _save_regen_prompt(job_dir, row):
    """ルート変更再生成で作った prompt を prompts.json に保存して次回再生成でも使えるようにする。"""
    import json as _json
    prompts_path = job_dir / "prompts.json"
    data = load_json(prompts_path, {"rows": []})
    rows = data.get("rows", [])
    replaced = False
    for i, existing in enumerate(rows):
        if existing.get("no") == row.get("no"):
            merged = dict(existing)
            merged.update(row)
            rows[i] = merged
            replaced = True
            break
    if not replaced:
        rows.append(row)
    data["rows"] = rows
    try:
        prompts_path.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _forced_route_user_instructions(force_route: str, base_instructions: str = "") -> str:
    """ルート変更再生成でも通常生成のチャンネル指示を維持し、必要な差分だけ足す。"""
    base = (base_instructions or "").strip()
    common = "Regenerate this item using the forced route/type. "
    if force_route == "realphoto":
        route_note = (
            common +
            "Create a photorealistic documentary-style image. "
            "Do not make an illustration, diagram, chart, graph, map, or icon layout. "
            "Do not add Japanese labels inside the image."
        )
    elif force_route == "illustration":
        route_note = (
            common +
            "Create a clear educational illustration. "
            "Do not make a chart, graph, map, or realistic photo."
        )
    elif force_route == "diagram":
        route_note = (
            common +
            "Create a simple, clear diagram. Do not make a chart, graph, map, realistic photo, "
            "or generic illustration. Keep the channel's normal visual style and quality rules."
        )
    else:
        route_note = common
    return f"{base}\n\n{route_note}".strip() if base else route_note


def _regenerate_render_chart(job_dir, no, snap_row, ch_keys, defaults, extra="", force_route=None, route_reason=None):
    """v3: chart 行（決定論レンダ）を再生成。

    1) 保存済み chart_spec があり追加指示が無ければ、その spec をそのまま描き直す
       （決定論・確実に同じグラフ）。
    2) 保存 spec が無い（旧ジョブ）or 追加指示あり → 文（＋指示）から spec を抽出し直す。
       追加指示は block_context に足すので、ユーザーが数値を補えば原文照合も通る。
    """
    from renderer import render_chart
    row = snap_row or {}
    chart_theme = defaults.get("chart_theme")
    out = job_dir / "images" / f"{no}.png"
    saved_spec = row.get("chart_spec")

    # 1) 保存 spec をそのまま再描画（追加指示が無いとき）
    if saved_spec and not extra:
        try:
            if render_chart(saved_spec, out, theme=chart_theme):
                _update_regen_snapshot(job_dir, no, True, filename=f"{no}.png", engine="render",
                                      route=force_route, route_reason=route_reason)
                return jsonify({"ok": True, "no": no, "filename": f"{no}.png",
                                "route": force_route,
                                "ts": datetime.now().strftime("%H%M%S")})
        except Exception:
            pass  # 失敗したら抽出し直しへフォールバック

    # 2) 抽出し直し（旧ジョブ or 追加指示で数値・体裁を変えたいとき）
    from utils import get_anthropic_client
    from router import extract_chart_specs
    sentence = (row.get("sentence") or "").strip()
    if not sentence and not saved_spec:
        return jsonify({"error": "グラフ再生成に必要な文が見つかりません（データが消えた可能性）"}), 404
    ctx = row.get("block_text") or ""
    if extra:
        ctx = (ctx + "\n" + extra).strip()  # 追加指示を文脈に（数値を補える＝原文照合も通る）
    spec = None
    if sentence:
        try:
            client = get_anthropic_client(ch_keys.get("anthropic", ""))
            specs = extract_chart_specs(client, [{"no": no, "sentence": sentence, "block_text": ctx}])
            spec = specs.get(no)
        except Exception as e:
            if not saved_spec:
                return jsonify({"error": f"グラフの数値抽出に失敗: {str(e)[:140]}"}), 500
    spec = spec or saved_spec  # 抽出できなければ保存 spec にフォールバック
    if not spec:
        return jsonify({"error": "この文からグラフ化できる数値が読み取れませんでした。再生成ダイアログに数値（例: ロシア 6.3%, NATO 2.1%）を書くと作り直せます。"}), 422
    try:
        ok = bool(render_chart(spec, out, theme=chart_theme))
    except Exception as e:
        return jsonify({"error": f"グラフ描画に失敗: {str(e)[:140]}"}), 500
    if not ok:
        return jsonify({"error": "グラフ描画に失敗しました"}), 500
    _update_regen_snapshot(job_dir, no, True, filename=f"{no}.png", engine="render",
                          route=force_route, route_reason=route_reason)
    return jsonify({"ok": True, "no": no, "filename": f"{no}.png",
                    "route": force_route, "ts": datetime.now().strftime("%H%M%S")})


def _regenerate_render_map(job_dir, no, snap_row, ch_keys, defaults, extra="", force_route=None, route_reason=None):
    """v3: map 行（決定論レンダ）を再生成。保存 map_spec を優先、無ければ抽出し直す。"""
    from renderer import render_map
    row = snap_row or {}
    chart_theme = defaults.get("chart_theme")
    out = job_dir / "images" / f"{no}.png"
    saved_spec = row.get("map_spec")

    if saved_spec and not extra:
        try:
            if render_map(saved_spec, out, theme=chart_theme):
                _update_regen_snapshot(job_dir, no, True, filename=f"{no}.png", engine="render",
                                      route=force_route, route_reason=route_reason)
                return jsonify({"ok": True, "no": no, "filename": f"{no}.png",
                                "route": force_route,
                                "ts": datetime.now().strftime("%H%M%S")})
        except Exception:
            pass

    from utils import get_anthropic_client
    from router import extract_map_specs
    sentence = (row.get("sentence") or "").strip()
    if not sentence and not saved_spec:
        return jsonify({"error": "地図再生成に必要な文が見つかりません（データが消えた可能性）"}), 404
    ctx = row.get("block_text") or ""
    if extra:
        ctx = (ctx + "\n" + extra).strip()
    spec = None
    if sentence:
        try:
            client = get_anthropic_client(ch_keys.get("anthropic", ""))
            specs = extract_map_specs(client, [{"no": no, "sentence": sentence, "block_text": ctx}])
            spec = specs.get(no)
        except Exception as e:
            if not saved_spec:
                return jsonify({"error": f"地図の地名抽出に失敗: {str(e)[:140]}"}), 500
    spec = spec or saved_spec
    if not spec:
        return jsonify({"error": "この文から地図化できる国・地域が特定できませんでした。再生成ダイアログに国名を書くと作り直せます。"}), 422
    try:
        ok = bool(render_map(spec, out, theme=chart_theme))
    except Exception as e:
        return jsonify({"error": f"地図描画に失敗: {str(e)[:140]}"}), 500
    if not ok:
        return jsonify({"error": "地図描画に失敗しました（対象の国/地域を特定できませんでした）"}), 500
    _update_regen_snapshot(job_dir, no, True, filename=f"{no}.png", engine="render",
                          route=force_route, route_reason=route_reason)
    return jsonify({"ok": True, "no": no, "filename": f"{no}.png",
                    "route": force_route, "ts": datetime.now().strftime("%H%M%S")})


def _regenerate_web_photo(job_dir, no, snap_row, ch_keys, defaults):
    """force_route=web_photo 用: 1件だけWeb画像検索してサムネイルを保存する。"""
    from utils import get_anthropic_client
    from web_searcher import run_web_search_for_selections, download_thumbnail
    row = snap_row or {}
    sentence = (row.get("sentence") or "").strip()
    if not sentence:
        return jsonify({"error": "Web写真検索に必要な文が見つかりません"}), 404
    client = get_anthropic_client(ch_keys.get("anthropic", ""))
    selections = [{"no": no, "query": sentence[:40], "topic": sentence[:24]}]
    results = run_web_search_for_selections(
        client,
        selections,
        max_workers=1,
        log=lambda *args, **kwargs: None,
        profile=defaults.get("web_search_profile", ""),
    )
    info = results[0] if results else {}
    thumb_url = info.get("thumb_url", "")
    if not thumb_url:
        return jsonify({"error": "Web写真候補は見つかりましたが、表示用サムネイルを取得できませんでした"}), 422
    fname = f"{no}.jpg"
    if not download_thumbnail(thumb_url, job_dir / "images" / fname):
        return jsonify({"error": "Web写真サムネイルの保存に失敗しました"}), 500
    _update_regen_snapshot(
        job_dir,
        no,
        True,
        filename=fname,
        engine="web",
        route="web_photo",
        route_reason="ルート違いから Web写真へ変更して再取得",
        extra={
            "web_source_url": info.get("source_url", ""),
            "web_thumb_url": thumb_url,
            "web_local_file": fname,
            "web_topic": info.get("topic", ""),
            "web_source_title": info.get("source_title", ""),
            "web_source_type": info.get("source_type", ""),
        },
    )
    return jsonify({"ok": True, "no": no, "filename": fname, "route": "web_photo", "ts": datetime.now().strftime("%H%M%S")})


@app.route("/api/regenerate/<job_id>/<int:no>", methods=["POST"])
@login_required
def api_regenerate(job_id, no):
    """指定シーン(№)の画像を1枚だけ作り直す。

    - chart（決定論レンダ）: 文から spec を再抽出して renderer で描き直す
    - それ以外（illustration / realphoto / diagram など）: AI で作り直す
    任意で extra_instruction（追加指示）を受け取り、プロンプト末尾に足して再生成できる。
    """
    from generator import run_parallel_generation, PROVIDER_NANOBANANA
    job_dir = OUTPUT_DIR / job_id
    if not job_dir.exists():
        return jsonify({"error": "ジョブのデータがサーバー上にありません（再デプロイ等で消えた可能性）。お手数ですが再生成してください。"}), 404

    # manifest があればそれを、無ければ job.json を params に使う（生成中でも動くように）
    manifest = load_json(job_dir / "manifest.json", {})
    job_state = load_json(job_dir / "job.json", {})
    params = manifest or job_state
    prompts = load_json(job_dir / "prompts.json", {"rows": []}).get("rows", [])
    snap_all = load_json(job_dir / "rows_progress.json", {"rows": []}).get("rows", [])
    target = next((r for r in prompts if r.get("no") == no), None)
    snap_row = next((r for r in snap_all if r.get("no") == no), None)
    extra = (request.form.get("extra_instruction", "") or "").strip()
    force_route = (request.form.get("force_route", "") or "").strip()
    diagram_edit = request.form.get("diagram_edit", "") == "1"
    forceable_routes = {"web_photo", "realphoto", "diagram", "chart", "illustration", "skip"}
    if force_route and force_route not in forceable_routes:
        return jsonify({"error": f"この再生成で指定できないルートです: {force_route}"}), 400

    # チャンネル設定・キーを解決（再生成も該当チャンネルの設定で）
    channel_id = params.get("channel_id", "default")
    channel = get_channel(channel_id)
    ch_keys = resolve_channel_keys(channel)
    defaults = channel.get("defaults", {}) or {}
    allow_maps = bool(defaults.get("allow_maps", False))

    # 対象行のルート/エンジン（snapshot 優先：chart/map は render エンジン）
    route = (snap_row or {}).get("route") or (target or {}).get("route") or (target or {}).get("type") or ""
    engine = (snap_row or {}).get("engine") or ""
    original_route = route
    if diagram_edit and not force_route:
        force_route = "diagram"
    if force_route:
        route = force_route
        engine = "ai"

    route_reason = ""
    if force_route:
        route_reason = f"{original_route or 'unknown'} から {force_route} へルート変更して再生成"
    elif route == "map" and not allow_maps:
        force_route = "diagram"
        route = "diagram"
        engine = "ai"
        route_reason = "地図なし設定: map から位置関係図解へ再生成"

    if force_route == "skip":
        _update_regen_snapshot(
            job_dir,
            no,
            True,
            engine="none",
            route="skip",
            route_reason=route_reason,
            status="skipped",
        )
        return jsonify({"ok": True, "no": no, "route": "skip", "skipped": True, "ts": datetime.now().strftime("%H%M%S")})
    if force_route == "web_photo":
        return _regenerate_web_photo(job_dir, no, snap_row, ch_keys, defaults)
    if force_route == "chart":
        return _regenerate_render_chart(job_dir, no, snap_row, ch_keys, defaults, extra,
                                        force_route="chart", route_reason=route_reason)
    # ===== v3: chart は決定論レンダ（AIプロンプトを持たない）→ 抽出し直して描き直す =====
    if not force_route and route == "chart" and (engine == "render" or defaults.get("chart_engine") == "render"):
        return _regenerate_render_chart(job_dir, no, snap_row, ch_keys, defaults, extra)
    if allow_maps and not force_route and route == "map" and (engine == "render" or defaults.get("map_engine") == "render"):
        return _regenerate_render_map(job_dir, no, snap_row, ch_keys, defaults, extra)

    # ===== AI 生成（illustration / realphoto / diagram など）=====
    if (not target or not target.get("prompt")) and not force_route:
        msg = ""
        if not prompts:
            msg = "プロンプト情報が見つかりません（まだ生成準備中か、データが消えています）"
        else:
            msg = f"№{no} のAI生成用プロンプトが見つかりません（chart/map は数値・地名のある文のみ再生成可）"
        _mark_regen_failed(job_dir, no, msg, route=route, route_reason=route_reason)
        return jsonify({"error": msg}), 404

    route = route or "illustration"
    if force_route and (not target or not target.get("prompt") or original_route != force_route):
        from utils import get_anthropic_client
        from prompter import generate_all_prompts
        source_row = dict(snap_row or target or {})
        sentence = (source_row.get("sentence") or "").strip()
        if not sentence:
            msg = "ルート変更再生成に必要な文が見つかりません"
            _mark_regen_failed(job_dir, no, msg, route=route, route_reason=route_reason)
            return jsonify({"error": msg}), 404
        source_row.update({
            "no": no,
            "route": force_route,
            "type": force_route,
            "sentence": sentence,
            "block_text": source_row.get("block_text", ""),
            "chapter_title": source_row.get("chapter_title", ""),
        })
        try:
            client = get_anthropic_client(ch_keys.get("anthropic", ""))
            base_user_instructions = (
                params.get("user_instructions")
                or defaults.get("user_instructions", "")
                or ""
            )
            regen_worldview_desc = (
                params.get("worldview_desc")
                or defaults.get("worldview_desc", "")
                or ""
            )
            generated_prompts = generate_all_prompts(
                client,
                [source_row],
                title=params.get("title") or "センテンス図解",
                user_instructions=_forced_route_user_instructions(force_route, base_user_instructions),
                style_preset=params.get("style_preset", "flat_infographic"),
                worldview_desc=regen_worldview_desc,
                max_workers=1,
                log=lambda *args, **kwargs: None,
            )
        except Exception as e:
            generated_prompts = []
            source_row["prompt"] = (
                "Create a simple, clear educational diagram that explains the following Japanese sentence. "
                "Use icons, arrows, and 2-4 labeled boxes. Do not create a chart or graph. "
                "Keep it easy to understand at a glance. Sentence: " + sentence
            )
            source_row["allowed_terms"] = []
            source_row["character"] = False
            source_row["type"] = force_route
            source_row["route"] = force_route
            source_row["prompt_error"] = str(e)[:120]
        target = (generated_prompts[0] if generated_prompts else source_row)
        target["route"] = force_route
        target["type"] = force_route
        _save_regen_prompt(job_dir, target)

    if diagram_edit:
        target = dict(target or snap_row or {"no": no})
        bp = dict(target.get("diagram_blueprint") or {})
        labels_raw = (request.form.get("diagram_labels", "") or "").strip()
        labels = [x.strip() for x in labels_raw.replace("、", ",").split(",") if x.strip()]
        structure = (request.form.get("diagram_structure", "") or "").strip()
        visual_goal = (request.form.get("diagram_visual_goal", "") or "").strip()
        relationships_raw = (request.form.get("diagram_relationships", "") or "").strip()
        relationships = [x.strip() for x in relationships_raw.splitlines() if x.strip()]
        if structure:
            bp["structure"] = structure
        if visual_goal:
            bp["visual_goal"] = visual_goal
        if labels:
            bp["labels"] = labels[:6]
            target["allowed_terms"] = labels[:6]
        if relationships:
            bp["relationships"] = relationships[:4]
        target["diagram_blueprint"] = bp
        target["route"] = "diagram"
        target["type"] = "diagram"
        route = "diagram"
        force_route = "diagram"
        engine = "ai"
        edit_note = (
            "Editor-adjusted diagram blueprint. Regenerate as a clear diagram using this blueprint exactly. "
            f"Structure: {bp.get('structure', '')}. Visual goal: {bp.get('visual_goal', '')}. "
            f"Labels: {', '.join(bp.get('labels', []))}. Relationships: {'; '.join(bp.get('relationships', []))}. "
            "Keep labels short, readable, non-overlapping, and only use the listed Japanese labels."
        )
        extra = f"{extra}\n{edit_note}".strip()
        route_reason = route_reason or "編集UIで図解設計を調整して再生成"
        _save_regen_prompt(job_dir, target)

    prompt_text = target.get("prompt", "")
    if extra:
        prompt_text = f"{prompt_text}\n\nAdditional instruction: {extra}"
    edit_image_path = _find_existing_scene_image(job_dir, no, snap_row=snap_row, target=target)
    if edit_image_path:
        prompt_text = (
            "Refine the attached existing image instead of generating a completely new one. "
            "Keep the same composition, style, main objects, and overall idea. "
            "Only make minimal improvements requested by the prompt, especially fixing "
            "Japanese text mistakes, text overlap, readability, and small layout defects.\n\n"
            + prompt_text
        )

    type_providers = params.get("type_providers") or defaults.get("type_providers") or {}
    provider = type_providers.get(route) or params.get("provider", PROVIDER_NANOBANANA)
    if provider not in VALID_PROVIDERS:
        provider = PROVIDER_NANOBANANA
    openai_quality = params.get("openai_quality") or "medium"
    style_preset = params.get("style_preset", "flat_infographic")

    # キャラ固定の参照画像（チャンネル設定）。先生が描かれる illustration のみ使用。
    character_ref_path = ""
    _cref = defaults.get("character_ref", "").strip()
    if _cref:
        _crp = PROJECT_ROOT / _cref
        if _crp.exists():
            character_ref_path = str(_crp)

    entry = {
        "index": no,
        "prompt": prompt_text,
        "type": route,
        "section": target.get("chapter_title", ""),
        "excerpt": target.get("sentence", ""),
        "keypoint": (target.get("sentence", "") or "")[:30],
        "allowed_terms": target.get("allowed_terms", []),
        "diagram_blueprint": target.get("diagram_blueprint", {}),
        "style": style_preset,
        "edit_source": bool(edit_image_path),
        # 元の行が先生シーン(character)なら、再生成でもキャラ固定
        "character": bool(target.get("character", False)) and route == "illustration",
    }

    _update_regen_snapshot(
        job_dir,
        no,
        True,
        engine="ai",
        route=route or None,
        route_reason=route_reason or "再生成中",
        status="generating",
        extra={
            "error": "",
            "regen_started_at": datetime.now().isoformat(timespec="seconds"),
        },
    )

    try:
        results = run_parallel_generation(
            prompts=[entry],
            output_dir=job_dir / "images",
            provider=provider,
            gemini_api_key=ch_keys.get("gemini") or None,
            openai_api_key=ch_keys.get("openai") or None,
            openai_quality=openai_quality,
            concurrency=1,
            reference_image_path=character_ref_path,
            edit_image_path=str(edit_image_path) if edit_image_path else None,
            realphoto_watermark=bool(defaults.get("realphoto_watermark", False)),
        )
    except Exception as e:
        msg = f"再生成に失敗: {str(e)[:150]}"
        _mark_regen_failed(job_dir, no, msg, route=route, route_reason=route_reason)
        return jsonify({"error": msg}), 500

    ok = bool(results and results[0].get("success") and results[0].get("filename"))
    filename = results[0].get("filename") if ok else None
    if not ok:
        msg = results[0].get("error", "生成失敗") if results else "生成失敗"
        if results and results[0].get("success") and not results[0].get("filename"):
            msg = "生成は完了しましたが、画像ファイル名が返りませんでした"
        _mark_regen_failed(job_dir, no, msg, route=route, route_reason=route_reason)
        return jsonify({"error": msg}), 500

    success_extra = {
        "error": "",
        "regen_finished_at": datetime.now().isoformat(timespec="seconds"),
        "verify_issue": False,
    }
    if diagram_edit:
        success_extra.update({
            "diagram_blueprint": target.get("diagram_blueprint", {}),
            "allowed_terms": target.get("allowed_terms", []),
        })

    # rows_progress.json を更新（AI 生成は engine=ai のまま）
    _update_regen_snapshot(
        job_dir,
        no,
        ok,
        filename=filename,
        engine="ai" if force_route else None,
        route=route or None,
        route_reason=route_reason or "再生成完了",
        extra=success_extra,
    )

    # キャッシュ回避用にタイムスタンプ付き URL を返す
    return jsonify({"ok": True, "no": no, "filename": filename, "route": route, "ts": datetime.now().strftime("%H%M%S")})


# v3 Step5: ルート違いフィードバック。編集者が「この文は別ルートが正しい」と教えると、
# チャンネル別の route_feedback.jsonl に蓄積し、次回以降のルーターに few-shot として渡す。
_FEEDBACK_ROUTES = ("web_photo", "realphoto", "diagram", "chart", "illustration", "skip")


@app.route("/api/feedback/<job_id>/<int:no>", methods=["POST"])
@login_required
def api_feedback(job_id, no):
    """指定シーン(№)のルート判定が間違っていたことを記録する。

    body: correct_route（正しいルート）。文・誤判定ルート・チャンネルは
    サーバー側のジョブデータから補完して route_feedback.jsonl に追記する。
    """
    import json as _json
    correct_route = (request.form.get("correct_route", "") or "").strip()
    if correct_route not in _FEEDBACK_ROUTES:
        return jsonify({"error": f"未知のルート: {correct_route}"}), 400

    job_dir = OUTPUT_DIR / job_id
    if not job_dir.exists():
        return jsonify({"error": "ジョブのデータが見つかりません"}), 404

    # 文・誤判定ルート・チャンネルをジョブデータから取得
    prompts = load_json(job_dir / "prompts.json", {"rows": []}).get("rows", [])
    target = next((r for r in prompts if r.get("no") == no), None)
    if target is None:
        snap = load_json(job_dir / "rows_progress.json", {"rows": []})
        target = next((r for r in snap.get("rows", []) if r.get("no") == no), None)
    if target is None:
        return jsonify({"error": f"№{no} が見つかりません"}), 404

    sentence = (target.get("sentence", "") or "").strip()
    given_route = (target.get("route") or target.get("type") or "").strip()
    if given_route == correct_route:
        return jsonify({"error": "同じルートです"}), 400

    manifest = load_json(job_dir / "manifest.json", {})
    job_state = load_json(job_dir / "job.json", {})
    channel_id = manifest.get("channel_id") or job_state.get("channel_id") or "default"

    record = {
        "channel_id": channel_id,
        "sentence": sentence[:200],
        "given_route": given_route,
        "correct_route": correct_route,
        "job_id": job_id,
        "no": no,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    fb_path = OUTPUT_DIR / "route_feedback.jsonl"
    try:
        with FEEDBACK_LOCK:
            with open(fb_path, "a", encoding="utf-8") as f:
                f.write(_json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        return jsonify({"error": f"保存に失敗: {str(e)[:150]}"}), 500

    return jsonify({"ok": True, "no": no, "given_route": given_route, "correct_route": correct_route})


@app.route("/results/<job_id>/<path:filename>")
@login_required
def serve_results(job_id, filename):
    result_dir = OUTPUT_DIR / job_id
    if not result_dir.exists():
        return "結果が見つかりません", 404
    return send_from_directory(str(result_dir), filename)


@app.route("/download/csv/<job_id>")
@login_required
def download_csv(job_id):
    """CSV を直接ダウンロード（Excel/Sheets 用 UTF-8 BOM 付き）"""
    csv_path = OUTPUT_DIR / job_id / "result.csv"
    if not csv_path.exists():
        return "CSV が見つかりません", 404
    manifest = load_json(OUTPUT_DIR / job_id / "manifest.json", {})
    title = manifest.get("title", job_id)
    safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:50] or job_id
    return send_file(
        csv_path,
        mimetype="text/csv; charset=utf-8",
        as_attachment=True,
        download_name=f"{safe_title}_{job_id}.csv",
    )


def _rows_for_download(result_dir: Path) -> list:
    manifest = load_json(result_dir / "manifest.json", {})
    rows = manifest.get("rows") or []
    if rows:
        return rows
    return load_json(result_dir / "rows_progress.json", {"rows": []}).get("rows", [])


def _write_rows_csv_to_zip(zf: zipfile.ZipFile, rows: list, arcname: str):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["章", "ブロック", "センテンス", "№", "ビート", "推定開始", "ソース",
                "エンジン", "重要度", "表示", "画像", "URL", "URL種別", "ライセンス", "クレジット"])
    route_labels = getattr(SentencePipeline, "ROUTE_LABELS", {})
    disp = {"image": "画像", "hold": "継続", "none": "なし"}
    for r in rows:
        w.writerow([
            r.get("chapter_title", "") if r.get("sentence_index") == 0 else "",
            r.get("block_text", "") if r.get("sentence_index") == 0 else "",
            r.get("sentence", ""),
            r.get("no", ""),
            "" if r.get("beat_id") is None else r.get("beat_id"),
            r.get("est_start", "") or "",
            route_labels.get(r.get("route", ""), r.get("route", "")),
            r.get("engine", "") or "",
            r.get("importance", "") or "",
            disp.get(r.get("display", ""), "画像" if r.get("filename") else ""),
            r.get("filename", "") or r.get("web_local_file", "") or "",
            r.get("web_source_url", "") or r.get("commons_page_url", "") or "",
            r.get("web_source_type", "") or "",
            r.get("license", "") or "",
            r.get("attribution", "") or "",
        ])
    zf.writestr(arcname, "\ufeff" + buf.getvalue())


def _send_temp_zip(tmp_path: str, download_name: str):
    resp = send_file(
        tmp_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=download_name,
    )

    @resp.call_on_close
    def _cleanup():
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    return resp


@app.route("/download/block/<job_id>/<int:chapter_index>/<int:block_index>")
@login_required
def download_block_zip(job_id, chapter_index, block_index):
    """指定ブロックの画像 + CSV + manifest をZIPでダウンロード。後工程で結合しやすい単位。"""
    result_dir = _safe_job_dir(job_id)
    if not result_dir or not result_dir.exists():
        return "結果が見つかりません", 404

    rows = [
        r for r in _rows_for_download(result_dir)
        if int(r.get("chapter_index") or 0) == chapter_index
        and int(r.get("block_index") or 0) == block_index
    ]
    if not rows:
        return "ブロックが見つかりません", 404

    manifest = load_json(result_dir / "manifest.json", {})
    title = manifest.get("title", job_id)
    safe_title = _safe_download_name(title, job_id)
    block_title = (rows[0].get("block_text") or f"block_{block_index + 1}").strip()
    safe_block = _safe_download_name(block_title, f"block_{block_index + 1}")
    prefix = f"ch{chapter_index:02d}_block{block_index + 1:03d}"

    import tempfile
    tmp = tempfile.NamedTemporaryFile(prefix=f"{job_id}_{prefix}_", suffix=".zip", delete=False)
    tmp_path = tmp.name
    tmp.close()

    image_names = []
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:
        images_dir = result_dir / "images"
        for r in rows:
            for key in ("filename", "web_local_file"):
                fname = r.get(key) or ""
                if not fname:
                    continue
                img = images_dir / Path(str(fname)).name
                if img.exists() and img.is_file() and img.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                    arc = f"images/{img.name}"
                    if arc not in image_names:
                        zf.write(img, arc)
                        image_names.append(arc)

        _write_rows_csv_to_zip(zf, rows, "block.csv")
        zf.writestr("block_manifest.json", json.dumps({
            "job_id": job_id,
            "title": title,
            "chapter_index": chapter_index,
            "block_index": block_index,
            "block_order_key": prefix,
            "block_title": block_title,
            "sentence_nos": [r.get("no") for r in rows],
            "image_files": image_names,
            "merge_hint": "Sort blocks by block_order_key, then concatenate rows/images in sentence_nos order.",
        }, ensure_ascii=False, indent=2))
        zf.writestr("README.txt", (
            "このZIPはセンテンスつくーるのブロック単位出力です。\n"
            "後で結合する場合は block_manifest.json の block_order_key 順に並べ、"
            "block.csv と images/ を結合してください。\n"
        ))

    return _send_temp_zip(
        tmp_path,
        f"{safe_title}_{job_id}_{prefix}_{safe_block}.zip",
    )


@app.route("/download/<job_id>")
@login_required
def download_zip(job_id):
    """画像一式 + CSV + manifest を ZIP でダウンロード"""
    result_dir = OUTPUT_DIR / job_id
    if not result_dir.exists():
        return "結果が見つかりません", 404

    manifest = load_json(result_dir / "manifest.json", {})
    title = manifest.get("title", job_id)
    safe_title = _safe_download_name(title, job_id)

    # ZIP はメモリ(BytesIO)ではなく一時ファイルに書き出す。
    # 大量画像(100枚超)を BytesIO に展開すると Render Free(512MB)で
    # メモリ超過 → ワーカー再起動 → ダウンロード中断/ログアウトの原因になるため。
    import tempfile
    tmp = tempfile.NamedTemporaryFile(prefix=f"{job_id}_", suffix=".zip", delete=False)
    tmp_path = tmp.name
    tmp.close()
    # 画像は既に圧縮済み(PNG/JPG)なので ZIP_STORED で CPU/メモリを節約
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:
        images_dir = result_dir / "images"
        if images_dir.exists():
            for img in sorted(images_dir.iterdir()):
                if img.is_file() and img.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                    zf.write(img, f"images/{img.name}")
        for extra, arc in [("result.csv", "result.csv"),
                           ("result.html", "result.html"),
                           ("manifest.json", "manifest.json"),
                           ("manuscript.txt", "manuscript.txt")]:
            p = result_dir / extra
            if p.exists():
                zf.write(p, arc)

    return _send_temp_zip(tmp_path, f"{safe_title}_{job_id}.zip")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3002))
    print("\n" + "=" * 50)
    print("  センテンスつくーる 起動中...")
    print(f"  http://localhost:{port}")
    print("=" * 50 + "\n")
    app.run(host="0.0.0.0", port=port, debug=False)
