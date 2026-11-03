"""Candidate discussions on the studio's private disk, independent of selection."""
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import material_store as store

LOCAL = "ローカル"
LIMIT = 3000
JST = timezone(timedelta(hours=9))
PUBLIC = ("id", "no", "candidate_id", "candidate_sha256", "parent_id", "author_name",
          "body", "created_at", "edited_at", "status", "resolved_by", "resolved_at",
          "status_by", "status_at", "withdrawn", "revision")


def load(job_dir):
    state = store.read(Path(job_dir) / "material_comments.json",
                       {"version": 1, "revision": 0, "comments": [], "requests": {}})
    if (not isinstance(state, dict) or state.get("version") != 1 or type(state.get("revision")) is not int
            or state["revision"] < 0 or not isinstance(state.get("requests"), dict)
            or not isinstance(state.get("comments"), list)):
        raise ValueError("コメントの保存内容を確認できません。上書きせず管理者にご連絡ください")
    by_id = {}
    for c in state["comments"]:
        if (not isinstance(c, dict) or not all(k in c for k in (*PUBLIC, "author_email"))
                or not all(isinstance(c[k], str) for k in ("id","no","candidate_id","author_name","author_email","body","created_at"))
                or type(c["revision"]) is not int or not 0 < c["revision"] <= state["revision"]
                or type(c["withdrawn"]) is not bool or c["status"] not in ("open","resolved") or c["id"] in by_id):
            raise ValueError("コメントの記録を読み取れません。保存内容を確認してください")
        if c["parent_id"] is not None:
            parent = by_id.get(c["parent_id"]) if isinstance(c["parent_id"], str) else None
            if not parent or parent["parent_id"] or (parent["no"], parent["candidate_id"]) != (c["no"], c["candidate_id"]):
                raise ValueError("返信先の保存内容を確認できません")
        by_id[c["id"]] = c
    return state


def _text(value, limit, label):
    if not isinstance(value, str) or not 0 < len(value.strip()) <= limit:
        raise ValueError(f"{label}は1〜{limit}字で入力してください")
    value = value.strip()
    if re.search(r"[\ud800-\udfff]", value):
        raise ValueError(f"{label}に使用できない文字があります")
    if re.search(r"[\x00-\x08\x0b-\x1f\x7f]", value.replace("\r\n", "\n")):
        raise ValueError(f"{label}に使用できない文字があります")
    return value.replace("\r\n", "\n")


def _operation(actor, target, body):
    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,64}", request_id):
        raise ValueError("操作番号を確認できません。画面を開き直してください")
    key = hashlib.sha256((actor + "\0" + request_id).encode()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps([target, body], sort_keys=True).encode()).hexdigest()
    return key, fingerprint


def _replay(requests, key, fingerprint):
    receipt = requests.get(key)
    if receipt is not None:
        if receipt["fingerprint"] != fingerprint:
            raise ValueError("この操作番号は別の内容で使用されています")
        return receipt["result"]


def display_name(root, actor, body=None):
    path = Path(root) / "material_people.json"
    with store.LOCK:
        people = store.read(path, {})
        if not isinstance(people, dict):
            raise ValueError("表示名の保存内容を確認できません")
        if body is None:
            return {"display_name": people.get(actor, LOCAL if actor == LOCAL else "")}
        name = _text(body.get("display_name"), 40, "表示名")
        if "@" in name or "\n" in name or "\t" in name:
            raise ValueError("表示名にはメールアドレスや改行を使えません")
        key, fingerprint = _operation(actor, "name", body)
        requests = people.setdefault("_requests", {})
        if not isinstance(requests, dict):
            raise ValueError("表示名の保存内容を確認できません")
        prior = _replay(requests, key, fingerprint)
        if prior is not None:
            return prior
        people[actor] = name
        result = {"display_name": name}
        requests[key] = {"fingerprint": fingerprint, "result": result}
        store.save(path, people)
        return result


def listing(job_dir, actor, since=None):
    with store.LOCK:
        state = load(job_dir)
    revision = state["revision"]
    if since is not None and not re.fullmatch(r"[0-9]{1,18}", str(since)):
        raise ValueError("コメントの版番号が不正です")
    cursor = int(since) if since is not None else -1
    reset = cursor < 0 or cursor > revision
    return {"revision": revision, "reset": reset,
            "comments": [{**{k: c[k] for k in PUBLIC}, "mine": c["author_email"] == actor}
                         for c in state["comments"] if reset or c["revision"] > cursor]}


def _find(comments, comment_id):
    row = next((c for c in comments if c["id"] == comment_id), None)
    if row is None:
        raise ValueError("コメントが見つかりません")
    return row


def _create(job_dir, comments, actor, name, body):
    if len(comments) >= LIMIT:
        raise ValueError(f"この案件のコメントは返信を含めて{LIMIT}件までです")
    no = str(body.get("no", ""))
    candidate = store.candidate(store.load_library(job_dir), no, body.get("candidate_id"))
    parent_id = body.get("parent_id") or None
    if parent_id:
        parent = _find(comments, parent_id)
        if parent["parent_id"] or parent["candidate_id"] != candidate["id"] or parent["withdrawn"]:
            raise ValueError("返信先のコメントと画像を確認してください")
    row = {"id": "c_" + uuid.uuid4().hex, "no": no, "candidate_id": candidate["id"],
           "candidate_sha256": candidate["sha256"], "parent_id": parent_id,
           "author_email": actor, "author_name": name, "body": _text(body.get("body"), 1000, "本文"),
           "created_at": store.now(), "edited_at": None, "withdrawn": False, "status": "open",
           "resolved_by": None, "resolved_at": None, "status_by": None, "status_at": None}
    comments.append(row)
    return row


def change(job_dir, root, actor, body, comment_id=None, *, status=False):
    key, fingerprint = _operation(actor, [comment_id, status], body)
    with store.LOCK:
        state = load(job_dir)
        prior = _replay(state["requests"], key, fingerprint)
        if prior is not None:
            return prior
        name = display_name(root, actor)["display_name"]
        if not name:
            raise ValueError("最初に表示名を入力してください")
        comments = state["comments"]
        if not comment_id:
            row = _create(job_dir, comments, actor, name, body)
        elif status:
            row = _find(comments, comment_id)
            value = body.get("status")
            if row["parent_id"] or value not in ("open", "resolved"):
                raise ValueError("対応状態は親コメントで選んでください")
            if row["status"] != value:
                stamp = store.now()
                row.update(status=value, status_by=name, status_at=stamp,
                           resolved_by=name if value == "resolved" else None,
                           resolved_at=stamp if value == "resolved" else None)
        else:
            row = _find(comments, comment_id)
            if row["author_email"] != actor or row["withdrawn"]:
                raise ValueError("自分の取り消していないコメントだけ変更できます")
            action = body.get("action")
            if action not in ("edit", "withdraw"):
                raise ValueError("コメントの操作が不正です")
            row.update(body=_text(body.get("body"), 1000, "本文") if action == "edit" else "",
                       withdrawn=action == "withdraw", edited_at=store.now())
        state["revision"] += 1
        row["revision"] = state["revision"]
        result = {"id": row["id"], "revision": row["revision"]}
        state["requests"][key] = {"fingerprint": fingerprint, "result": result}
        store.save(Path(job_dir) / "material_comments.json", state)
        return result


def counts(job_dir):
    comments = load(job_dir)["comments"]
    live_threads = {c["parent_id"] or c["id"] for c in comments if not c["withdrawn"]}
    result = {"open": 0, "resolved": 0}
    for c in comments:
        if c["id"] in live_threads:
            result[c["status"]] += 1
    return result


def export_lines(job_dir, mode, library):
    if mode not in ("all", "open", "none"):
        raise ValueError("書き出すコメントの範囲を選んでください")
    by_scene = {}
    if mode == "none":
        return by_scene
    comments = load(job_dir)["comments"]
    parents = {c["id"]: c for c in comments if not c["parent_id"]}
    for c in comments:  # Stored in chronological order, including replies.
        parent = parents[c["parent_id"] or c["id"]]
        if mode == "open" and (parent["status"] != "open" or c["withdrawn"]):
            continue
        no = c["no"]
        candidates = library["candidates"].get(no) or []
        current = library["selections"].get(no) or (candidates[-1]["id"] if candidates else "")
        old = "（前の候補へ）" if c["candidate_id"] != current else ""
        date = datetime.fromisoformat(c["created_at"]).astimezone(JST)
        label = "済" if parent["status"] == "resolved" else "未対応"
        body = "取り消されたコメント" if c["withdrawn"] else c["body"]
        reply = "↳ " if c["parent_id"] else ""
        by_scene.setdefault(no, []).append(f"{reply}[{label}] {c['author_name']} {date.month}/{date.day}{old}：{body}")
    return {no: "\n".join(lines) for no, lines in by_scene.items()}
