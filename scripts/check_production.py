"""Offline checks used both by GitHub CI and Render before every replacement."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile

TESTS = ["migration_entry", "deploy_guard", "auth_json", "block_download",
         "recent_jobs", "key_attribution", "resume", "job_recovery",
         # 2026-09-25 世界観ロック（カラクリ経済学の画風統一）: 渡し忘れ・判定の読み違いを本番前に止める
         "style_lock", "style_check", "flag_check", "upload_ui", "materials", "generation_queue",
         "compare", "scene_fix", "web_pick", "regenerate_chart", "source_pack", "otona_pilot", "otona_styles", "suasi_poses",
         # Cloudflare の証明を毎回要求し、旧入口・旧セッションの迂回を拒否
         "cloudflare_access",
         # 2026-10-14 時間のかかる4つの操作を「受け付け → 問い合わせ」で（新しい入口は応答を約100秒しか待たない）
         "studio_operations",
         # 2026-10-14 地名が出るだけで地図にしない（図解スタジオと同じ判断）
         "map_fit",
         # 2026-10-14 章の先頭・素材なしの後で、存在しない画像を継続しない
         "allocator", "non_image_rows", "image_memory", "image_files",
         "result_boundaries", "safe_images"]

if __name__ == "__main__":
    # Build containers cannot mount /data. Tests must never inherit production
    # credentials or use production storage, even when invoked from Render.
    environment = {k: v for k, v in os.environ.items()
                   if not k.endswith(("_KEY", "_TOKEN", "_PASSWORD", "_HOOK"))
                   and not k.startswith("CF_ACCESS_")
                   and k not in {"SUPABASE_URL", "DATA_DIR", "RENDER_SERVICE_ID"}}
    with tempfile.TemporaryDirectory(prefix="sentence-checks-") as root:
        environment.update(DATA_DIR=root, SECRET_KEY="offline-tests-only", APP_PASSWORD="offline-tests-only")
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                 *[f"tests/test_{name}.py" for name in TESTS]], env=environment,
                                cwd=Path(__file__).resolve().parent.parent)
    raise SystemExit(result.returncode)
