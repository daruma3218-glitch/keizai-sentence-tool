"""時間のかかる操作を「受け付け → 問い合わせ」で返す（2026-10-14）。

新しい入口（sozai.apprendre.jp）は Cloudflare を通る。Cloudflare は元のサーバーの応答を約100秒しか待たず、
場面の直し・その修正・作り直し・素材の別案はそれを超えることがある（切れても裏の処理は続くが、画面に結果が届かない）。
そこで、画面が要求に X-Studio-Async: 1 と操作の番号（X-Studio-Operation）を付けた時だけ、
  1. 受け付けてすぐ 202 と番号を返し、
  2. 届いた要求をそのまま（本文・Content-Type・クエリも同じ）このプロセスの中で裏で動かし
     （今までと同じ入口・同じログインと CSRF の確認・同じ処理を通す）、
  3. 結果（今までと同じ JSON と状態コード）を <OUTPUT_ROOT>/operations/<番号>.json に残す。
画面は GET /api/operations/<番号> を数秒ごとに問い合わせ、終わったら今までと同じように結果を扱う。
- 印を付けない古い画面・試験は、今までどおり待って返す（4つの入口の中身は変えていない）。
- 同じ番号をもう一度送っても二度は動かない（受け付けの返事だけが届かなかった時のため）。
- 同時に動かすのは limit() 件まで（既定3・場面の直しは1件で最大3枚を並べて作るため）。超えた分は順番待ちにし、
  受け付けるのは queue_limit() 件まで（既定12）。それを超えたら「混み合っています」（429）。
- 動いている間・順番待ちの間は、反映（deploy_guard）を待たせる（running_count）。
- 再起動で止まった「実行中」（持ち主のプロセスが無い・始めてから MAX_AGE_SECONDS を超えた）は「中断」として返し、
  自動では作り直さない（作り直しの行・素材の別案の受付は失敗に戻す）。
- 記録に残すのは結果（今までの応答と同じ中身）だけ。送られた画像そのものは残さない。2日たった記録は消す。
"""
import io
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import job_recovery
import material_store as store

ASYNC_HEADER = "X-Studio-Async"
ID_HEADER = "X-Studio-Operation"
REPLAY_KEY = "studio.operation"  # 裏で動かす要求の目印（WSGI の environ。外から来る要求には付けられない）
OPERATION_ID = re.compile(r"[A-Za-z0-9_-]{16,64}")
_JOB = r"([A-Za-z0-9][A-Za-z0-9_-]{0,95})"
PATHS = (
    ("scene_fix", re.compile(r"/api/scene-fix")),
    ("scene_fix_revise", re.compile(r"/api/scene-fix/" + _JOB + r"/revise")),
    ("regenerate", re.compile(r"/api/regenerate/" + _JOB + r"/(\d{1,6})")),
    ("variant", re.compile(r"/api/material-jobs/" + _JOB + r"/variant")),
)
# 裏の要求に渡す見出し。ログイン（セッション・新しい入口の証明）と素材画面の CSRF だけ
FORWARD_HEADERS = ("Cookie", "X-Material-CSRF", "Cf-Access-Jwt-Assertion", "User-Agent", "Accept-Language")
KEEP_SECONDS = 2 * 24 * 3600
PRUNE_EVERY_SECONDS = 3600
MAX_AGE_SECONDS = 45 * 60  # 別のプロセスの「実行中」をこれより長く待たない（プロセス番号の使い回しへの備え）
INTERRUPTED = "サーバーの再起動で中断しました。自動では作り直しません。結果を確かめてから、もう一度操作してください。"
UNREADABLE = "操作の記録を読めませんでした。画面を再読み込みして結果を確かめてください。"

_LOCK = threading.Lock()
_SLOT = threading.Condition(_LOCK)  # 同時に動かす数の待ち合わせ（_LOCK を共有）
_RUNNING = {}       # 番号 → 種類（このプロセスで受け付けたもの。順番待ちも含む）
_STARTED = set()    # そのうち、動き始めたもの
_UNSAVED = {}       # 結果の保存に失敗したもの（問い合わせにはここから返す。再起動で消える）
_PRUNED = {"at": 0.0}


def _env_int(name, default):
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def limit():
    """同時に動かす数。一括生成（1枠）とは別。以前は要求のスレッド（8本）が暗黙の上限だった。"""
    return _env_int("STUDIO_OPERATIONS_MAX", 3)


def queue_limit():
    """受け付ける数（動いているもの＋順番待ち）。"""
    return max(limit(), _env_int("STUDIO_OPERATIONS_QUEUE", 12))


def running_count():
    """受け付けて、まだ結果を保存し終えていない数（順番待ちを含む）。反映はこれが0になるまで待つ。"""
    with _LOCK:
        return len(_RUNNING)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _folder(module):
    return Path(module.OUTPUT_ROOT) / "operations"


def _record_path(module, op_id):
    return _folder(module) / (op_id + ".json")


def _read(module, op_id):
    for attempt in range(20):
        try:
            return json.loads(_record_path(module, op_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except PermissionError:
            time.sleep(0.025)  # Windows で置き換えの最中（本番の Linux では起きない）
        except (OSError, ValueError):
            break
    return {"id": op_id, "status": "unreadable"}


def _write(module, op_id, record):
    """壊れにくく書く（一時ファイル → 置き換え）。Windows では読み手が開いている間は置き換えられないので少し待つ。"""
    path = _record_path(module, op_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        for attempt in range(40):
            try:
                os.replace(temp, path)
                return
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(0.025)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def match(path):
    """裏で動かせる入口なら（種類, 部分）を返す。それ以外は (None, ())。"""
    for kind, pattern in PATHS:
        found = pattern.fullmatch(path or "")
        if found:
            return kind, found.groups()
    return None, ()


def _capture(request):
    """届いた要求をそのまま写す（本文・Content-Type・クエリ）。作り直さないので、ファイル名などの書き方も同じになる。

    本文は先に読み切って控える（元の要求が終わると読めなくなるため）。form はこの控えから読める。
    大きすぎる要求は、ここで今までどおり 413 になる。読めなかった時は None（今までどおり待って返す）。
    """
    body = request.get_data(cache=True, parse_form_data=False)
    if request.content_length and len(body) != request.content_length:
        return None
    headers = {name: request.headers[name] for name in FORWARD_HEADERS if name in request.headers}
    return {"path": request.path, "query": request.environ.get("QUERY_STRING", ""), "base_url": request.host_url,
            "headers": headers, "body": body, "content_type": request.headers.get("Content-Type", "")}


def _result(code, content_type, body):
    if "json" in (content_type or ""):
        try:
            return {"status": code, "body": json.loads(body.decode("utf-8"))}
        except (UnicodeDecodeError, ValueError):
            pass
    return {"status": code, "text": body[:4000].decode("utf-8", "replace")}


def _replay(module, op_id, captured):
    from werkzeug.test import EnvironBuilder, run_wsgi_app
    try:
        builder = EnvironBuilder(path=captured["path"], base_url=captured["base_url"],
                                 query_string=captured["query"], method="POST", headers=captured["headers"],
                                 input_stream=io.BytesIO(captured["body"]), content_length=len(captured["body"]),
                                 content_type=captured["content_type"] or None,
                                 environ_overrides={REPLAY_KEY: op_id})
        try:
            environ = builder.get_environ()
        finally:
            builder.close()
        app_iter, status, headers = run_wsgi_app(module.app.wsgi_app, environ, buffered=True)
        try:
            body = b"".join(app_iter)
        finally:
            if hasattr(app_iter, "close"):
                app_iter.close()
        return _result(int(str(status).split(" ", 1)[0]), headers.get("Content-Type", ""), body)
    except Exception:
        # 例外の文には鍵や外部の応答が混じりうるので、ログにも結果にも出さない
        logging.error("A studio operation stopped with an unexpected error")
        return {"status": 500, "body": {"ok": False, "error": "処理の途中で問題が起きました。結果を確かめてから、もう一度操作してください。"}}


def _run(module, record, captured):
    op_id = record["id"]
    try:
        with _SLOT:
            while len(_STARTED) >= limit():
                _SLOT.wait(timeout=5)
            _STARTED.add(op_id)
        started = time.monotonic()
        try:
            result = _replay(module, op_id, captured)
        finally:
            with _SLOT:
                _STARTED.discard(op_id)
                _SLOT.notify_all()
        record.update(status="done", finished_at=_now(), seconds=round(time.monotonic() - started, 1), result=result)
        try:
            _write(module, op_id, record)
        except Exception:
            logging.error("Could not save a studio operation result")
            with _LOCK:
                _UNSAVED[op_id] = dict(record)  # 問い合わせにはここから返す（「中断」と見誤らない）
        # 運用で見る行（Render の Logs）。番号の頭と結果だけ
        print(f"[operation] {record['kind']} {op_id[:8]} finished {result['status']} in {record['seconds']}s", flush=True)
    finally:
        # どんな抜け方でも一覧から外す（外れないと、反映が再起動まで止まる）
        with _LOCK:
            _RUNNING.pop(op_id, None)


def start(module, request, kind, parts):
    """受け付けて裏で動かす。戻り値は (応答の dict, 状態コード)。要求を写せない時は None（今までどおり待って返す）。"""
    op_id = request.headers.get(ID_HEADER, "")
    if not OPERATION_ID.fullmatch(op_id):
        return {"ok": False, "error": "画面を再読み込みしてから、もう一度操作してください。"}, 400
    captured = _capture(request)  # 大きすぎる要求はここで 413（Flask がそのまま返す）
    if captured is None:
        return None
    variant_request_id = request.form.get("request_id", "") if kind == "variant" else ""
    with _LOCK:
        if op_id in _RUNNING or op_id in _UNSAVED:
            return _accepted(op_id, "running" if op_id in _RUNNING else "done")
        existing = _read(module, op_id)
        if existing:
            return _accepted(op_id, existing.get("status") or "running")
        if len(_RUNNING) >= queue_limit():
            return {"ok": False, "code": "operations_busy",
                    "error": "時間のかかる操作が混み合っています。少し待ってから、もう一度押してください。"}, 429
        _RUNNING[op_id] = kind
    try:
        record = {"id": op_id, "kind": kind, "path": request.path, "status": "running",
                  "owner": job_recovery.runtime_owner(), "started_at": _now()}
        if kind != "scene_fix":
            record["job_id"] = parts[0]
        if kind == "regenerate":
            record["no"] = int(parts[1])
        if kind == "variant" and store.ID.fullmatch(variant_request_id):
            record["variant_request_id"] = variant_request_id
        _write(module, op_id, record)
        threading.Thread(target=_run, args=(module, record, captured), daemon=True, name="studio-operation").start()
    except Exception:
        with _LOCK:
            _RUNNING.pop(op_id, None)
        logging.error("Could not start a studio operation")
        return {"ok": False, "error": "受け付けられませんでした。もう一度押してください。"}, 500
    _prune(module)
    return _accepted(op_id, "running")


def _accepted(op_id, status):
    return {"ok": True, "operation": {"id": op_id, "status": status, "poll": "/api/operations/" + op_id}}, 202


def _age_seconds(record):
    try:
        return time.time() - datetime.fromisoformat(record["started_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def _abandoned(record):
    """このプロセスで動いていない「実行中」が、もう進まないか。

    このプロセスの記録（結果を書く前に止まった）・持ち主のプロセスがもう無い・始めてから長すぎる（プロセス番号の
    使い回しで「生きている」と見誤る時への備え）のどれか。
    """
    owner = record.get("owner")
    me = job_recovery.runtime_owner()
    if isinstance(owner, dict) and owner.get("pid") == me["pid"] and owner.get("nonce") == me["nonce"]:
        return True
    if not job_recovery.owner_alive(owner):
        return True
    age = _age_seconds(record)
    return age is None or age > MAX_AGE_SECONDS


def _cleanup(module, record):
    """中断した操作が残した「作成中」を失敗に戻す（画面が作成中のまま固まらないように）。作り直しはしない。"""
    try:
        job_dir = module._safe_job_dir(record.get("job_id", "")) if record.get("job_id") else None
        if not job_dir or not job_dir.is_dir():
            return
        if record.get("kind") == "regenerate" and isinstance(record.get("no"), int):
            rows = (module.load_json(job_dir / "rows_progress.json", {"rows": []}) or {}).get("rows", [])
            row = next((r for r in rows if r.get("no") == record["no"]), None)
            if row and row.get("status") == "generating":
                module._mark_regen_failed(job_dir, record["no"], "サーバーの再起動で作り直しが中断しました。もう一度押してください。")
        elif record.get("kind") == "variant" and record.get("variant_request_id"):
            with store.LOCK:
                lib = store.load_library(job_dir)
                pending = lib["requests"].get(record["variant_request_id"])
                if pending and pending.get("status") == "running":
                    pending.update(status="failed", finished_at=store.now(), interrupted=True)
                    store.commit_library(job_dir, lib, "operation_interrupted",
                                         {"request_id": record["variant_request_id"]})
    except Exception:
        logging.error("Could not reset a studio operation left running")


def _interrupt_if_abandoned(module, op_id):
    with _LOCK:
        if op_id in _RUNNING or op_id in _UNSAVED:
            return _UNSAVED.get(op_id) or _read(module, op_id)
        record = _read(module, op_id)
        if not record or record.get("status") != "running" or not _abandoned(record):
            return record
        record.update(status="interrupted", finished_at=_now())
        try:
            _write(module, op_id, record)
        except OSError:
            logging.error("Could not save an interrupted studio operation")
    _cleanup(module, record)
    return record


def status(module, op_id):
    """問い合わせの応答。戻り値は (応答の dict, 状態コード)。"""
    if not OPERATION_ID.fullmatch(op_id or ""):
        return {"ok": False, "error": "この操作は受け付けられていません。もう一度押してください。"}, 404
    with _LOCK:
        running = op_id in _RUNNING
        waiting = running and op_id not in _STARTED
        record = _UNSAVED.get(op_id)
    if record is None:
        record = _read(module, op_id)
    if record is None:
        if running:  # 受け付けた直後（記録を書く前）
            return {"ok": True, "id": op_id, "status": "running", "waiting": waiting, "elapsed": 0}, 200
        return {"ok": False, "error": "この操作は受け付けられていません。もう一度押してください。"}, 404
    if record.get("status") == "running" and not running:
        record = _interrupt_if_abandoned(module, op_id) or record
    state = record.get("status")
    if state == "running":
        age = _age_seconds(record)
        return {"ok": True, "id": op_id, "status": "running", "waiting": waiting,
                "elapsed": None if age is None else max(0, int(age))}, 200
    if state == "done" and isinstance(record.get("result"), dict):
        return {"ok": True, "id": op_id, "status": "done", "result": record["result"]}, 200
    if state == "interrupted":
        return {"ok": True, "id": op_id, "status": "interrupted", "error": INTERRUPTED}, 200
    # 読めない記録は終わった扱いにする（画面が待ち続けないように）
    return {"ok": True, "id": op_id, "status": "unknown", "error": UNREADABLE}, 200


def _prune(module, force=False):
    now = time.time()
    if not force and now - _PRUNED["at"] < PRUNE_EVERY_SECONDS:
        return
    _PRUNED["at"] = now
    folder = _folder(module)
    if not folder.is_dir():
        return
    with _LOCK:
        keep = set(_RUNNING)
    for path in folder.glob("*.json"):
        try:
            if path.stem not in keep and now - path.stat().st_mtime > KEEP_SECONDS:
                path.unlink()
        except OSError:
            continue


def sweep(module):
    """起動した時に、前のプロセスが残した「実行中」を「中断」にする（作成中の行・受付も失敗に戻す）。"""
    folder = _folder(module)
    if not folder.is_dir():
        return []
    changed = []
    for path in sorted(folder.glob("*.json")):
        if not OPERATION_ID.fullmatch(path.stem):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("status") == "running":
            if (_interrupt_if_abandoned(module, path.stem) or {}).get("status") == "interrupted":
                changed.append(path.stem)
    _prune(module, force=True)
    return changed


def register(module):
    from flask import jsonify, request, session
    app = module.app
    try:
        changed = sweep(module)
        if changed:
            logging.warning("Marked %d studio operations left running by a stopped process as interrupted", len(changed))
    except Exception:
        logging.error("Studio operation check at startup failed")

    def respond(payload, code):
        response = jsonify(payload)
        response.status_code = code
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.before_request
    def accept_studio_operation():
        if (request.method != "POST" or request.headers.get(ASYNC_HEADER) != "1"
                or request.environ.get(REPLAY_KEY)):
            return None
        kind, parts = match(request.path)
        if not kind:
            return None
        # ログインと素材画面の確認に通らない要求は受け付けない（今までどおりの 401・403 をその場で返す）
        if module.APP_PASSWORD and not session.get("authenticated"):
            return None
        if kind == "variant":
            import secrets
            expected = session.get("material_csrf", "")
            if not expected or not secrets.compare_digest(expected, request.headers.get("X-Material-CSRF", "")):
                return None
        accepted = start(module, request, kind, parts)
        return respond(*accepted) if accepted else None

    @app.route("/api/operations/<op_id>")
    @module.login_required
    def studio_operation_status(op_id):
        return respond(*status(module, op_id))
