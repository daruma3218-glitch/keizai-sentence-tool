"""Read-only index of studio projects and pre-existing sentence jobs."""
from datetime import datetime, timedelta, timezone

import material_store as store

JST = timezone(timedelta(hours=9))
STATUS = {
    "queued": ("順番待ち", "queued"),
    "running": ("制作中", "working"),
    "completed": ("生成完了", "done"),
    "error": ("処理エラー", "error"),
    "failed": ("生成失敗", "error"),
    "interrupted": ("中断", "attention"),
    "cancelled": ("停止済み", "neutral"),
    "unknown": ("状態不明", "neutral"),
}


def _read(path, issues):
    try:
        data = store.read(path, {})
        if not isinstance(data, dict):
            raise ValueError("Unexpected snapshot")
        return data
    except (OSError, ValueError):
        issues.add(str(path))
        return {}


def _stamp(records, paths):
    stamps = []
    for record in records:
        for key in ("updated_at", "created_at"):
            try:
                value = datetime.fromisoformat(record[key].replace("Z", "+00:00"))
                stamps.append(value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp())
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
                pass
    if stamps:
        return max(stamps)
    for path in paths:
        try:
            stamps.append(path.stat().st_mtime)
        except OSError:
            pass
    return max(stamps, default=0)


def _number(value):
    try:
        return max(0, int(value))
    except (ValueError, TypeError, OverflowError):
        return 0


def index(module, channel_id):
    """Do not bind legacy jobs, copy images, or start generation while browsing."""
    issues, projects, jobs = set(), [], []
    project_for_job = {}
    for path in (module.OUTPUT_ROOT / "material_projects").glob("*.json"):
        project = _read(path, issues)
        if project.get("channel_id") != channel_id:
            continue
        project_id = project.get("project_id", "")
        if not isinstance(project_id, str) or not store.ID.fullmatch(project_id):
            issues.add(str(path))
            continue
        ids = project.get("jobs", [])
        ids = ids if isinstance(ids, list) else []
        project = {"project_id": project_id, "title": project.get("title") or "名称未設定の案件",
                   "source_channel_id": store.channel(channel_id)["source_id"],
                   "trello_url": project.get("trello_url", ""),
                   "created_at": project.get("created_at", ""),
                   "jobs": [item for item in ids if isinstance(item, str) and store.ID.fullmatch(item)]}
        projects.append(project)
        for job_id in project["jobs"]:
            project_for_job[job_id] = project

    folders = module.OUTPUT_DIR.iterdir() if module.OUTPUT_DIR.exists() else []
    for folder in folders:
        if not folder.is_dir() or not store.ID.fullmatch(folder.name) or folder.is_symlink():
            continue
        paths = [folder / name for name in ("job.json", "manifest.json", "material_context.json")]
        state, manifest, context = [_read(path, issues) for path in paths]
        if manifest.get("tool") == "scene_fix" or folder.name.startswith("scene_fix_"):
            continue
        project = project_for_job.get(folder.name, {})
        source = (context.get("channel_id") or context.get("source_channel_id") or
                  manifest.get("channel_id") or state.get("channel_id") or project.get("source_channel_id"))
        if not isinstance(source, str) or store.ALIASES.get(source, source) != channel_id:
            continue
        if not (state or manifest or context):
            issues.add(str(folder))
            continue
        library_path = folder / "material_library.json"
        library = _read(library_path, issues)
        updated = _stamp((state, manifest, context, library, project), [*paths, library_path])
        status = state.get("status") or ("completed" if manifest else "unknown")
        if not isinstance(status, str):
            status = "unknown"
        label, tone = STATUS.get(status, STATUS["unknown"])
        # A broken state must not be inferred as complete from an older manifest.
        if str(paths[0]) in issues:
            status, label, tone = "unknown", *STATUS["unknown"]
        completed = status == "completed"
        selections = library.get("selections", {})
        selected = len(selections) if isinstance(selections, dict) else 0
        resumable = (status in {"error", "failed", "interrupted", "cancelled"}
                     and not paths[1].exists() and (folder / "manuscript.txt").is_file()
                     and str(paths[0]) not in issues)
        jobs.append({
            "id": folder.name,
            "title": (context.get("title") or manifest.get("title") or state.get("title") or
                      state.get("title_override") or project.get("title") or folder.name),
            "status": status, "status_label": label, "tone": tone,
            "bucket": "completed" if completed else "unfinished",
            "generated": _number(manifest.get("generated", state.get("generated", 0))),
            "total": _number(manifest.get("total_sentences", state.get("total_sentences", 0))),
            "selected": selected, "resumable": resumable,
            "source_label": "素材スタジオ" if context or project else "従来のセンテンス",
            "href": ("/materials/jobs/" if completed else "/progress/") + folder.name,
            "action": "素材選びを続ける →" if completed else "進捗・素材を見る →",
            "updated": updated,
            "date": datetime.fromtimestamp(updated, JST).strftime("%Y/%m/%d %H:%M") if updated else "日時不明",
            "project_id": project.get("project_id") or context.get("project_id", ""),
            "trello_url": project.get("trello_url") or context.get("trello_url", ""),
        })
    jobs.sort(key=lambda item: (item["updated"], item["id"]), reverse=True)
    present = {item["id"] for item in jobs}
    drafts = []
    for project in projects:
        if not present.intersection(project["jobs"]):
            drafts.append({**project, "missing_jobs": bool(project["jobs"])})
    drafts.sort(key=lambda item: item["created_at"], reverse=True)
    return {"jobs": jobs, "drafts": drafts, "warning": bool(issues),
            "unfinished": sum(item["bucket"] == "unfinished" for item in jobs),
            "completed": sum(item["bucket"] == "completed" for item in jobs)}
