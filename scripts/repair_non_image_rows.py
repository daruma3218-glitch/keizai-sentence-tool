"""Offline, narrowly scoped repair. Dry-run by default; backs up before --apply."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from row_generation import repair_false_failures


def repair_job(job_dir, apply=False):
    job_dir = Path(job_dir)
    state = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    if state.get("status") not in ("completed", "interrupted", "failed"):
        raise ValueError("Stop the active job before repairing its snapshot")
    path = job_dir / "rows_progress.json"
    original = path.read_bytes()
    snapshot = json.loads(original)
    prompts = json.loads((job_dir / "prompts.json").read_text(encoding="utf-8")).get("rows", [])
    repaired = repair_false_failures(snapshot, prompts)
    if apply and repaired:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        backup = job_dir / f"rows_progress.before-non-image-repair-{stamp}.json"
        shutil.copy2(path, backup)
        # Refuse to overwrite a concurrently changed snapshot.
        if path.read_bytes() != original:
            raise RuntimeError("Snapshot changed while preparing repair")
        temporary = job_dir / f"rows_progress.repair-{stamp}.tmp"
        temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    return {"job": job_dir.name, "repaired": len(repaired), "rows": repaired, "applied": bool(apply and repaired)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(repair_job(args.job_dir, args.apply), ensure_ascii=False))
