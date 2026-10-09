"""Versioned materials shared by the studio, editing and project tracking.

Existing pipeline files are inputs, never rewritten by this module.
The serving application currently uses one worker; locks cover its threads.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from PIL import Image

VERSION = "2026-10-09.2"
LOCK = threading.RLock()
CHANNELS = {
    "otona": {"id": "otona", "source_id": "otona", "name": "大人の学び直しTV", "color": "#305a75",
               "mark": "学", "default_mode": "sentence", "description": "仕組み説明の素材を少数試作。必要な場面の原稿を入れ、採用するものを選びます。"},
    "russia": {"id": "russia", "source_id": "roshia", "name": "ロシア解体新書", "color": "#a32b35",
               "mark": "ろ", "logo": "channel-logos/roshia.png", "default_mode": "diagram", "description": "図解を作り、候補を選ぶ。原稿に沿った制作も選べます。"},
    "economy": {"id": "economy", "source_id": "keizai", "name": "日本カラクリ経済学", "color": "#28785c",
                "mark": "経", "logo": "channel-logos/keizai.png", "default_mode": "sentence", "description": "先生の世界観を保ち、原稿に沿って素材をそろえます。"},
    "china": {"id": "china", "source_id": "china", "name": "中国チャンネル", "color": "#227887",
              "mark": "中", "default_mode": "sentence", "description": "これからの制作に。画風と生成設定を準備してから始めます。"},
    "success": {"id": "success", "source_id": "seikou", "name": "成功の法則", "color": "#b86429",
                "mark": "成", "default_mode": "sentence", "description": "チャンネルの設定を使い、場面と素材をまとめて管理します。"},
}
ALIASES = {v["source_id"]: k for k, v in CHANNELS.items()}
ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}$")
EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def now():
    return datetime.now(timezone.utc).isoformat()


def channel(value):
    key = ALIASES.get(value, value)
    if key not in CHANNELS:
        raise ValueError("チャンネルを選び直してください")
    return CHANNELS[key]


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return copy.deepcopy(default)
    except json.JSONDecodeError:
        raise ValueError("制作データの更新中です。少し待って読み込み直してください")


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_path(root, relative):
    root = Path(root).resolve()
    raw = Path(str(relative))
    if raw.is_absolute() or ".." in raw.parts or "\\" in str(relative) or ":" in str(relative):
        raise ValueError("素材の場所が不正です")
    path = (root / raw).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("素材の場所が不正です")
    return path


def project_file(output_root, project_id):
    if not ID.fullmatch(str(project_id or "")):
        raise ValueError("案件番号が不正です")
    return Path(output_root) / "material_projects" / (project_id + ".json")


def get_project(output_root, project_id):
    result = read(project_file(output_root, project_id))
    if not result:
        raise ValueError("案件が見つかりません")
    return result


def card_url(trello_url):
    trello_url = str(trello_url or "").strip()
    if trello_url:
        u = urlparse(trello_url)
        match = re.fullmatch(r"/c/([A-Za-z0-9]{8})(?:/[^/?#]*)?/?", u.path)
        if u.scheme != "https" or u.netloc != "trello.com" or not match:
            raise ValueError("TrelloカードのURLを確認してください")
        trello_url = "https://trello.com/c/" + match[1]
    return trello_url


def create_project(output_root, channel_id, title, defaults, trello_url=""):
    ch = channel(channel_id)
    title = str(title).strip()[:160]
    if not title:
        raise ValueError("動画のタイトルを入力してください")
    trello_url = card_url(trello_url)
    project_id = ("trello-" + trello_url.rsplit("/", 1)[1]) if trello_url else "ms-" + uuid.uuid4().hex
    with LOCK:
        path = project_file(output_root, project_id)
        existing = read(path)
        if existing:
            if existing["channel_id"] != ch["id"]:
                raise ValueError("このTrelloカードは別チャンネルの案件です")
            return existing
        profile = copy.deepcopy(defaults)
        profile["coverage_mode"] = "scene"
        profile_hash = hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        result = {"schema_version": 1, "project_id": project_id, "channel_id": ch["id"],
                  "source_channel_id": ch["source_id"], "title": title, "trello_url": trello_url,
                  "profile_version": VERSION, "profile_sha256": profile_hash, "profile": profile,
                  "created_at": now(), "jobs": []}
        save(path, result)
        return result


def bind_job(output_root, job_dir, project):
    with LOCK:
        latest = get_project(output_root, project["project_id"])
        if Path(job_dir).name not in latest["jobs"]:
            latest["jobs"].append(Path(job_dir).name)
        save(project_file(output_root, latest["project_id"]), latest)
        context = {k: v for k, v in latest.items() if k != "jobs"}
        context.update(tool="material-studio", source_tool="sentence", job_id=Path(job_dir).name)
        save(Path(job_dir) / "material_context.json", context)
        return context


def source_rows(job_dir):
    data = read(Path(job_dir) / "rows_progress.json", {}) or {}
    rows = data.get("rows") or (read(Path(job_dir) / "manifest.json", {}) or {}).get("rows", [])
    return sorted(rows, key=lambda r: int(r.get("no") or 0))


def load_library(job_dir):
    return read(Path(job_dir) / "material_library.json",
                {"schema_version": 1, "revision": 0, "candidates": {}, "selections": {},
                 "display": {}, "flags": {}, "history": [], "requests": {}})


def commit_library(job_dir, library, action, detail=None):
    library["revision"] += 1
    library["updated_at"] = now()
    library["history"].append({"at": now(), "revision": library["revision"],
                               "action": action, **(detail or {})})
    save(Path(job_dir) / "material_library.json", library)


def snapshot(job_dir, library, no, path, label, metadata=None):
    path = Path(path)
    if path.suffix.lower() not in EXTENSIONS or not path.is_file():
        raise ValueError("画像が見つかりません")
    try:
        with Image.open(path) as image:
            image.verify()
    except (OSError, SyntaxError):
        raise ValueError("画像を読み込めません。生成中の場合は少し待って読み込み直してください")
    checksum = digest(path)
    blob = Path(job_dir) / "material_assets" / (checksum + path.suffix.lower())
    blob.parent.mkdir(exist_ok=True)
    if not blob.exists():
        temp = blob.with_name(uuid.uuid4().hex + ".tmp")
        shutil.copyfile(path, temp)
        if digest(temp) != checksum:
            temp.unlink()
            raise ValueError("画像が更新中です。少し待って読み込み直してください")
        temp.replace(blob)
    entry = {"id": str(no) + "-" + checksum, "sha256": checksum,
             "filename": blob.relative_to(job_dir).as_posix(), "label": label,
             "created_at": now(), **(metadata or {})}
    variants = library["candidates"].setdefault(str(no), [])
    previous = next((v for v in variants if v["id"] == entry["id"]), None)
    if not previous:
        variants.append(entry)
    return previous or entry


def sync_originals(job_dir, library):
    added = 0
    for row in source_rows(job_dir):
        filename = row.get("filename") or row.get("web_local_file")
        if not filename:
            continue
        path = safe_path(Path(job_dir) / "images", filename)
        if not path.is_file():
            continue
        no = str(row["no"])
        before = len(library["candidates"].get(no, []))
        snapshot(job_dir, library, no, path, "生成した素材",
                 {k: row.get(k, "") for k in ("web_source_url", "license", "attribution", "route")})
        added += len(library["candidates"][no]) - before
    return added


def candidate(library, no, candidate_id):
    found = next((v for v in library["candidates"].get(str(no), []) if v["id"] == candidate_id), None)
    if not found:
        raise ValueError("この場面の候補が見つかりません")
    return found


def selection_change(job_dir, body):
    with LOCK:
        lib = load_library(job_dir)
        if int(body.get("revision", -1)) != lib["revision"]:
            raise ValueError("別の操作で更新されています。読み込み直してください")
        no = str(body.get("no"))
        if not any(str(r.get("no")) == no for r in source_rows(job_dir)):
            raise ValueError("場面が見つかりません")
        action = body.get("action")
        if action == "select":
            picked = candidate(lib, no, body.get("candidate_id"))
            lib["selections"][no] = picked["id"]
            lib["display"][no] = "image"
        elif action == "clear":
            lib["selections"].pop(no, None)
        elif action == "flag":
            lib["flags"][no] = str(body.get("note") or "要修正")[:500]
        elif action == "unflag":
            lib["flags"].pop(no, None)
        elif action == "image":
            lib["display"][no] = "image"
        elif action in {"hold", "none"}:
            lib["display"][no] = action
            lib["selections"].pop(no, None)
        elif action == "undo":
            previous = read(Path(job_dir) / "material_previous.json")
            if not previous or previous["after_revision"] != lib["revision"]:
                raise ValueError("戻せる採用操作がありません")
            for key in ("selections", "display", "flags"):
                lib[key] = previous[key]
        else:
            raise ValueError("操作が不正です")
        old = load_library(job_dir)
        save(Path(job_dir) / "material_previous.json",
             {k: old[k] for k in ("selections", "display", "flags")} | {"after_revision": lib["revision"] + 1})
        commit_library(job_dir, lib, action, {"no": no})
        return lib


def selection_hash(job_dir, lib=None):
    """Stable export identity, excluding unused candidates and operation history."""
    lib = lib if lib is not None else load_library(job_dir)
    row_keys = ("no", "chapter_index", "block_index", "sentence", "display", "route", "est_start")
    selected = {no: candidate(lib, no, cid) for no, cid in lib["selections"].items()}
    contents = {"rows": [{k: row.get(k) for k in row_keys} for row in source_rows(job_dir)],
                "selections": selected, "display": lib["display"], "flags": lib["flags"],
                "source_job_status": (read(Path(job_dir) / "job.json", {}) or {}).get("status", "unknown")}
    return hashlib.sha256(json.dumps(contents, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def handoff(job_dir):
    context = read(Path(job_dir) / "material_context.json")
    if not context:
        raise ValueError("先にチャンネルと案件をひも付けてください")
    lib = load_library(job_dir)
    state = read(Path(job_dir) / "job.json", {}) or {}
    rows, missing, previous, previous_chapter = [], [], None, None
    for r in source_rows(job_dir):
        if previous_chapter != r.get("chapter_index", 0):
            previous = None
        previous_chapter = r.get("chapter_index", 0)
        no = str(r["no"])
        display = lib["display"].get(no, r.get("display") or ("none" if r.get("route") == "skip" else "image"))
        chosen = lib["selections"].get(no)
        asset = candidate(lib, no, chosen) if chosen else None
        if asset:
            path = safe_path(job_dir, asset["filename"])
            if not path.is_file() or digest(path) != asset["sha256"]:
                raise ValueError("採用素材の内容が変わっています")
        if display == "image":
            if not asset:
                missing.append({"no": r["no"], "reason": "素材の採用待ち"})
            previous = asset
        elif display == "hold" and previous is None:
            missing.append({"no": r["no"], "reason": "継続する前の素材がありません"})
        elif display == "none":
            previous = None
        elif display not in {"image", "hold", "none"}:
            missing.append({"no": r["no"], "reason": "表示方法を選んでください"})
        if lib["flags"].get(no):
            missing.append({"no": r["no"], "reason": lib["flags"][no]})
        rows.append({"no": r["no"], "chapter_index": r.get("chapter_index", 0),
                     "block_index": r.get("block_index", 0), "sentence": r.get("sentence", ""),
                     "display": display, "asset": asset if display == "image" else None,
                     "hold_asset_id": previous["id"] if display == "hold" and previous else None,
                     "estimated_start": r.get("est_start"), "timing_status": "narration_alignment_required"})
    status = state.get("status", "unknown")
    ready = bool(rows) and not missing and status == "completed"
    return {"schema": "material-studio-handoff", "schema_version": 1,
            "context": {k: v for k, v in context.items() if k != "profile"},
            "profile": context["profile"], "material_revision": lib["revision"],
            "selection_hash": selection_hash(job_dir, lib),
            "source_job_status": status, "status": "materials_ready" if ready else "selection_pending",
            "ready_for_editing": ready, "missing": missing, "rows": rows, "exported_at": now(),
            "approval": {"video_approved": False, "note": "素材の採用は動画の最終承認とは別"},
            "cost": {"amount": None, "status": "not_measured"},
            "operations": [{"request_id": key, **value} for key, value in lib["requests"].items()]}
