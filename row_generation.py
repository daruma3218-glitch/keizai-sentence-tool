"""Distinguish an allocated image from a sentence that only holds/skips it."""


def non_image_status(row, prompt=None):
    row = row or {}
    if (row.get("display") in ("hold", "none")
            and not row.get("filename")
            and not row.get("prompt")
            and not (prompt or {}).get("prompt")):
        return row["display"]
    return None


def repair_false_failures(snapshot, prompts):
    """Repair only the known no-prompt regeneration error; retain other fields."""
    targets = {r.get("no"): r for r in prompts}
    repaired = []
    for row in snapshot.get("rows", []):
        status = non_image_status(row, targets.get(row.get("no")))
        expected = (f"№{row.get('no')} のAI生成用プロンプトが見つかりません"
                    "（chart/map は数値・地名のある文のみ再生成可）")
        # _mark_regen_failed also sets verify_issue=True. Clear that synthetic
        # flag only when there is no independent image-review verdict to keep.
        if (status and row.get("status") == "failed"
                and row.get("error") == expected
                and not row.get("verify_reason") and not row.get("verify_status")
                and not row.get("verify_issue_tags")):
            row.update(status=status, error="")
            if "verify_issue" in row:
                row["verify_issue"] = False
            repaired.append(row["no"])
    return repaired
