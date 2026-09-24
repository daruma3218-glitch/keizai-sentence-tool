"""Offline checks used both by GitHub CI and Render before every replacement."""
from pathlib import Path
import subprocess
import sys

TESTS = ["migration_entry", "deploy_guard", "auth_json", "block_download",
         "recent_jobs", "key_attribution", "resume"]

if __name__ == "__main__":
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                             *[f"tests/test_{name}.py" for name in TESTS]],
                            cwd=Path(__file__).resolve().parent.parent)
    raise SystemExit(result.returncode)
