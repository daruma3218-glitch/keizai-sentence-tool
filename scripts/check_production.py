"""Offline checks used both by GitHub CI and Render before every replacement."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile

TESTS = ["migration_entry", "deploy_guard", "auth_json", "block_download",
         "recent_jobs", "key_attribution", "resume",
         # 2026-09-25 世界観ロック（カラクリ経済学の画風統一）: 渡し忘れ・判定の読み違いを本番前に止める
         "style_lock", "style_check", "flag_check", "upload_ui"]

if __name__ == "__main__":
    # Build containers cannot mount /data. Tests must never inherit production
    # credentials or use production storage, even when invoked from Render.
    environment = {k: v for k, v in os.environ.items()
                   if not k.endswith(("_KEY", "_TOKEN", "_PASSWORD", "_HOOK"))
                   and k not in {"SUPABASE_URL", "DATA_DIR"}}
    with tempfile.TemporaryDirectory(prefix="sentence-checks-") as root:
        environment.update(DATA_DIR=root, SECRET_KEY="offline-tests-only", APP_PASSWORD="offline-tests-only")
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                 *[f"tests/test_{name}.py" for name in TESTS]], env=environment,
                                cwd=Path(__file__).resolve().parent.parent)
    raise SystemExit(result.returncode)
