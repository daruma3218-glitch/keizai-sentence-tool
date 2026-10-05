"""Authenticated studio entry and immutable candidate selection."""
import csv
import io
import json
import secrets
import tempfile
import threading
import zipfile
from pathlib import Path

from flask import Blueprint, jsonify, redirect, render_template, request, send_file, session
from PIL import Image, ImageOps

import material_store as store
import material_history

IMAGE_SLOTS = threading.BoundedSemaphore(2)
ACTIVE = set()


def register(module):
    bp = Blueprint("materials", __name__)
    image_activity = {"count": 0}

    @module.app.before_request
    def track_image_work():
        path = request.path
        if request.method == "POST" and (path.startswith("/api/regenerate/") or path == "/api/scene-fix"
                or path.startswith("/api/scene-fix/") and path.endswith("/revise")
                or path.startswith("/api/material-jobs/") and path.endswith("/variant")):
            with store.LOCK:
                image_activity["count"] += 1
            request.environ["material.image_work"] = True

    @module.app.teardown_request
    def finish_image_work(error):
        if request.environ.pop("material.image_work", False):
            with store.LOCK:
                image_activity["count"] -= 1

    def board_snapshot():
        guard = getattr(module, "_deploy_guard", None)
        with store.LOCK:
            count = image_activity["count"]
        return {**module.generation_board(), "other_operations": count,
                "accepting": guard.intake_open() if guard else True}

    def token():
        if not session.get("material_csrf"):
            session["material_csrf"] = secrets.token_urlsafe(32)
        return session["material_csrf"]

    @bp.before_request
    def protect_write():
        if module.APP_PASSWORD and not session.get("authenticated") and request.path.startswith("/api/"):
            return jsonify(error="ログインし直してください"), 401
        if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
            expected = session.get("material_csrf", "")
            received = request.headers.get("X-Material-CSRF", "")
            if not expected or not secrets.compare_digest(expected, received):
                return jsonify(error="画面を再読み込みして操作してください"), 403

    @bp.errorhandler(ValueError)
    def bad_input(error):
        return jsonify(error=str(error)), 409

    def job(job_id):
        path = module._safe_job_dir(job_id)
        if not path or not path.is_dir():
            raise ValueError("制作データが見つかりません")
        return path

    def profile(canonical):
        ch = store.channel(canonical)
        return module.get_channel(ch["source_id"])

    def channels():
        result = []
        for ch in store.CHANNELS.values():
            config = profile(ch["id"])
            keys = module.resolve_channel_keys(config)
            result.append({**ch, "ready": bool(keys["openai"] or keys["gemini"])})
        return result

    @bp.route("/materials")
    @module.login_required
    def home():
        selected = request.args.get("channel")
        selected = store.channel(selected)["id"] if selected else None
        card = store.card_url(request.args.get("trello", ""))
        if card and selected:
            project = store.read(store.project_file(module.OUTPUT_ROOT, "trello-" + card.rsplit("/", 1)[1]))
            if project:
                if project["channel_id"] != selected:
                    raise ValueError("このカードは別チャンネルの案件です")
                history = material_history.index(module, selected)
                recent = next((j for j in history["jobs"] if j["project_id"] == project["project_id"]), None)
                return redirect(recent["href"] if recent else
                                "/?channel_id=" + project["source_channel_id"] + "&project_id=" + project["project_id"])
        return render_template("materials.html", channels=channels(), selected=selected,
                               history=material_history.index(module, selected) if selected else None, csrf=token(),
                               board=board_snapshot(),
                               prefill_title=request.args.get("title", "")[:160],
                               prefill_trello=card)

    @bp.route("/api/material-generation-board")
    @module.login_required
    def generation_board():
        response = jsonify(board_snapshot())
        response.headers["Cache-Control"] = "no-store"
        return response

    @bp.route("/materials/guide")
    @module.login_required
    def guide():
        return render_template("material_guide.html", csrf=token())

    @bp.route("/api/material-jobs/<job_id>/resume", methods=["POST"])
    @module.login_required
    def resume(job_id):
        d = job(job_id)
        # Reuse the existing checkpoint runner, with studio CSRF and click serialization.
        with store.LOCK:
            state = store.read(d / "job.json", {}) or {}
            if not isinstance(state, dict) or not isinstance(state.get("status", ""), str):
                raise ValueError("制作記録を読み込めません。画面を更新して確認してください")
            if state.get("status") not in {"error", "failed", "interrupted", "cancelled"}:
                raise ValueError("中断・失敗を確認できたジョブだけ再開できます。画面を更新してください")
            return module.api_resume(job_id)

    @bp.route("/api/material-projects", methods=["POST"])
    @module.login_required
    def create():
        body = request.get_json(silent=True) or {}
        ch = store.channel(body.get("channel_id"))
        result = store.create_project(module.OUTPUT_ROOT, ch["id"], body.get("title", ""),
                                      profile(ch["id"]).get("defaults", {}), body.get("trello_url", ""))
        return jsonify(project=result)

    @bp.route("/api/material-projects/<project_id>")
    @module.login_required
    def project(project_id):
        p = store.get_project(module.OUTPUT_ROOT, project_id)
        progress = []
        for job_id in p["jobs"]:
            d = module._safe_job_dir(job_id)
            if d and d.is_dir():
                h = store.handoff(d)
                progress.append({k: h[k] for k in ("context", "status", "ready_for_editing",
                                                  "material_revision", "source_job_status", "missing", "cost")})
        return jsonify(project={k: v for k, v in p.items() if k != "profile"}, jobs=progress)

    @bp.route("/materials/jobs/<job_id>")
    @module.login_required
    def library(job_id):
        d = job(job_id)
        context = store.read(d / "material_context.json")
        state = store.read(d / "job.json", {}) or {}
        manifest = store.read(d / "manifest.json", {}) or {}
        source_channel = (context or {}).get("source_channel_id") or manifest.get("channel_id") or state.get("channel_id")
        ch = store.channel(source_channel)
        return render_template("material_library.html", job_id=job_id, channel=ch, csrf=token(),
                               title=(context or {}).get("title") or manifest.get("title") or job_id)

    def payload(d):
        lib = store.load_library(d)
        receipt = store.read(d / "material_last_export.json")
        return {"library": lib, "rows": store.source_rows(d),
                "context": store.read(d / "material_context.json"),
                "export": {"last": receipt, "changed": bool(receipt and receipt.get("selection_hash") != store.selection_hash(d, lib))},
                "status": (store.read(d / "job.json", {}) or {}).get("status", "unknown")}

    @bp.route("/api/material-jobs/<job_id>")
    @module.login_required
    def get_library(job_id):
        return jsonify(payload(job(job_id)))

    @bp.route("/api/material-jobs/<job_id>/sync", methods=["POST"])
    @module.login_required
    def sync(job_id):
        d = job(job_id)
        with store.LOCK:
            if not (d / "material_context.json").exists():
                manifest = store.read(d / "manifest.json", {}) or {}
                state = store.read(d / "job.json", {}) or {}
                ch = store.channel(manifest.get("channel_id") or state.get("channel_id"))
                defaults = dict(profile(ch["id"]).get("defaults", {}))
                for key in ("worldview_desc", "style_preset", "provider", "openai_quality", "openai_model", "style_lock"):
                    if key in manifest or key in state:
                        defaults["openai_image_model" if key == "openai_model" else key] = manifest.get(key, state.get(key))
                p = store.create_project(module.OUTPUT_ROOT, ch["id"],
                                         manifest.get("title") or state.get("title_override") or job_id, defaults)
                store.bind_job(module.OUTPUT_ROOT, d, p)
            lib = store.load_library(d)
            count = store.sync_originals(d, lib)
            if count:
                store.commit_library(d, lib, "sync", {"added": count})
        return jsonify(payload(d))

    @bp.route("/api/material-jobs/<job_id>/selection", methods=["POST"])
    @module.login_required
    def select(job_id):
        store.selection_change(job(job_id), request.get_json(silent=True) or {})
        return jsonify(payload(job(job_id)))

    @bp.route("/materials/asset/<job_id>/<candidate_id>")
    @module.login_required
    def asset(job_id, candidate_id):
        d = job(job_id)
        lib = store.load_library(d)
        found = next((c for values in lib["candidates"].values() for c in values if c["id"] == candidate_id), None)
        if not found:
            raise ValueError("画像が見つかりません")
        return send_file(store.safe_path(d, found["filename"]), conditional=True, max_age=3600)

    @bp.route("/api/material-jobs/<job_id>/variant", methods=["POST"])
    @module.login_required
    def variant(job_id):
        d = job(job_id)
        body = request.form
        request_id = body.get("request_id", "")
        if not store.ID.fullmatch(request_id):
            raise ValueError("操作番号が不正です")
        kind = body.get("kind")
        if kind not in {"generate", "edit", "flip", "upload"}:
            raise ValueError("操作が不正です")
        no = str(body.get("no", ""))
        row = next((r for r in store.source_rows(d) if str(r.get("no")) == no), None)
        context = store.read(d / "material_context.json")
        if not row or not context:
            raise ValueError("素材を読み込んでから操作してください")
        extra = body.get("instruction", "").strip()[:2000]
        count = 2 if body.get("count") == "2" else 1
        if kind == "edit" and not extra:
            raise ValueError("直したい内容を入力してください")
        config = profile(context["channel_id"])
        keys = module.resolve_channel_keys(config)
        defaults = dict(context["profile"], **context.get("generation_settings", {}))
        route = row.get("route")
        if route not in {"diagram", "illustration", "realphoto"}:
            route = "diagram"
        if route == "realphoto" and not defaults.get("allow_ai_realphoto", True):
            route = "illustration"
        type_providers, _ = module._effective_type_providers(defaults, keys)
        provider = type_providers.get(route) or defaults.get("provider") or "gpt-image"
        if provider not in {"gpt-image", "nanobanana"}:
            raise ValueError("画像生成の設定を確認してください")
        if kind in {"generate", "edit"} and not keys["openai" if provider == "gpt-image" else "gemini"]:
            raise ValueError("このチャンネルの画像生成設定が未完了です")
        with store.LOCK:
            lib = store.load_library(d)
            old = lib["requests"].get(request_id)
            if old:
                if old["status"] == "completed":
                    return jsonify(payload(d))
                raise ValueError("この操作は受付済みです。結果を読み込み直してください。自動で再生成しません")
            if job_id in ACTIVE:
                raise ValueError("この案件の素材を処理中です")
            if int(body.get("revision", -1)) != lib["revision"]:
                raise ValueError("別の操作で更新されています。読み込み直してください")
            source = None
            if kind in {"edit", "flip"}:
                source = store.candidate(lib, no, body.get("candidate_id"))
            lib["requests"][request_id] = {"status": "running", "kind": kind, "no": no,
                                          "requested_images": count if kind in {"generate", "edit"} else 0,
                                          "started_at": store.now(), "cost_amount": None}
            store.commit_library(d, lib, "operation_started", {"request_id": request_id})
            ACTIVE.add(job_id)
        output = d / "material_operations" / request_id
        output.mkdir(parents=True, exist_ok=True)
        try:
            produced = []
            if kind == "upload":
                upload = request.files.get("image")
                if not upload:
                    raise ValueError("画像を選んでください")
                ext = Path(upload.filename or "").suffix.lower()
                if ext not in store.EXTENSIONS:
                    raise ValueError("PNG・JPEG・WebPを選んでください")
                target = output / ("uploaded" + ext)
                upload.save(target)
                with Image.open(target) as im:
                    im.verify()
                produced.append((target, "取り込んだ候補", {"origin": "upload"}))
            elif kind == "flip":
                target = output / "flipped.png"
                with Image.open(store.safe_path(d, source["filename"])) as im:
                    with ImageOps.mirror(im) as mirrored:
                        mirrored.save(target)
                produced.append((target, "左右反転", {"parent_id": source["id"]}))
            else:
                from generator import run_parallel_generation
                locked = defaults.get("worldview_desc", "") if defaults.get("style_lock") else ""
                prompts = [{"index": i, "prompt": module._build_scene_fix_prompt(
                    row.get("sentence", ""), route, i, extra, defaults, fix_mode="same_style",
                    style_locked=bool(locked)), "type": route, "section": row.get("chapter_title", ""),
                    "excerpt": row.get("sentence", ""), "keypoint": row.get("sentence", "")[:30],
                    "style": defaults.get("style_preset", "flat_infographic"), "character": False,
                    "edit_source": kind == "edit",
                    "allowed_terms": module._scene_fix_allowed_terms(row.get("sentence", ""), route)}
                    for i in range(1, count + 1)]
                reference = defaults.get("character_ref", "")
                reference = module.PROJECT_ROOT / reference if reference else None
                with IMAGE_SLOTS:
                    results = run_parallel_generation(
                        prompts=prompts, output_dir=output, provider=provider,
                        gemini_api_key=keys["gemini"], openai_api_key=keys["openai"],
                        openai_quality=defaults.get("openai_quality", "medium"),
                        openai_model=defaults.get("openai_image_model"),
                        style_preset=defaults.get("style_preset", "flat_infographic"), concurrency=1,
                        reference_image_path=str(reference) if reference and reference.exists() else None,
                        edit_image_path=str(store.safe_path(d, source["filename"])) if source else None,
                        realphoto_watermark=bool(defaults.get("realphoto_watermark")) and route == "realphoto",
                        style_lock_text=locked)
                for result in results:
                    if result.get("success") and result.get("filename"):
                        produced.append((store.safe_path(output, result["filename"]),
                                         "手直し案" if kind == "edit" else "追加の候補",
                                         {"parent_id": source["id"] if source else None, "origin": "ai",
                                          "provider": provider, "request_id": request_id}))
                if not produced:
                    raise ValueError("候補を作成できませんでした。元画像と採用状態は保持しています")
            with store.LOCK:
                lib = store.load_library(d)
                for path, label, metadata in produced:
                    store.snapshot(d, lib, no, path, label, metadata)
                lib["requests"][request_id].update(status="completed", finished_at=store.now(), created_images=len(produced))
                store.commit_library(d, lib, "operation_completed", {"request_id": request_id})
        except Exception:
            with store.LOCK:
                lib = store.load_library(d)
                lib["requests"][request_id].update(status="failed", finished_at=store.now())
                store.commit_library(d, lib, "operation_failed", {"request_id": request_id})
            raise ValueError("候補の作成に失敗しました。元画像・採用状態を保持しました。再読込で結果を確認してください")
        finally:
            with store.LOCK:
                ACTIVE.discard(job_id)
        return jsonify(payload(d))

    @bp.route("/api/material-jobs/<job_id>/handoff")
    @module.login_required
    def handoff(job_id):
        with store.LOCK:
            return jsonify(store.handoff(job(job_id)))

    @bp.route("/materials/jobs/<job_id>/download")
    @module.login_required
    def download(job_id):
        return build_download(job_id)

    @bp.route("/api/material-jobs/<job_id>/export", methods=["POST"])
    @module.login_required
    def export(job_id):
        body = request.get_json(silent=True) or {}
        return build_download(job_id, record=True, expected_revision=body.get("revision"),
                              expected_hash=body.get("selection_hash"))

    def build_download(job_id, record=False, expected_revision=None, expected_hash=None):
        d = job(job_id)
        with store.LOCK:
            data = store.handoff(d)
            if record and (expected_revision != data["material_revision"] or expected_hash != data["selection_hash"]):
                raise ValueError("書き出す内容が更新されました。もう一度内容を確認してください")
            tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
            tmp.close()
            with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED) as archive:
                archive.writestr("material-handoff.json", json.dumps(data, ensure_ascii=False, indent=2))
                stream = io.StringIO()
                writer = csv.writer(stream)
                writer.writerow(["章", "番号", "原稿", "表示", "採用画像", "出典", "クレジット", "版"])
                for row in data["rows"]:
                    asset = row["asset"] or {}
                    filename = asset.get("filename", "")
                    writer.writerow([row["chapter_index"], row["no"], row["sentence"], row["display"],
                                     filename, asset.get("web_source_url", ""), asset.get("attribution", ""), asset.get("sha256", "")])
                archive.writestr("採用素材.csv", "\ufeff" + stream.getvalue())
                for filename in {r["asset"]["filename"] for r in data["rows"] if r["asset"]}:
                    archive.write(store.safe_path(d, filename), filename)
                archive.writestr("README.txt", "採用した素材と原稿の対応です。推定時刻は音声に未同期です。\n"
                                 "ready_for_editing は素材の準備状態で、動画の最終承認ではありません。\n")
            if record:
                store.save(d / "material_last_export.json", {"revision": data["material_revision"],
                           "selection_hash": data["selection_hash"], "exported_at": data["exported_at"]})
        return module._send_temp_zip(tmp.name, "materials_" + job_id + ".zip")

    module.app.register_blueprint(bp)
