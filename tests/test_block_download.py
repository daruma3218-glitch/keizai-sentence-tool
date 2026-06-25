import io
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

import app as appmod  # noqa: E402


def _png(path):
    Image.new("RGB", (32, 18), (255, 255, 255)).save(path)


def test_download_block_zip_contains_only_target_block(tmp_path, monkeypatch):
    out_root = tmp_path / "output"
    job_dir = out_root / "job_block"
    images_dir = job_dir / "images"
    images_dir.mkdir(parents=True)
    monkeypatch.setattr(appmod, "OUTPUT_DIR", out_root)
    monkeypatch.setattr(appmod, "APP_PASSWORD", "")

    _png(images_dir / "1.png")
    _png(images_dir / "2.png")
    _png(images_dir / "3.png")
    rows = [
        {
            "no": 1,
            "chapter_index": 1,
            "block_index": 0,
            "sentence_index": 0,
            "chapter_title": "第1章",
            "block_text": "ブロックA",
            "sentence": "Aの文",
            "route": "diagram",
            "filename": "1.png",
            "status": "ok",
        },
        {
            "no": 2,
            "chapter_index": 1,
            "block_index": 0,
            "sentence_index": 1,
            "chapter_title": "第1章",
            "block_text": "ブロックA",
            "sentence": "Aの文2",
            "route": "diagram",
            "filename": "2.png",
            "status": "ok",
        },
        {
            "no": 3,
            "chapter_index": 1,
            "block_index": 1,
            "sentence_index": 0,
            "chapter_title": "第1章",
            "block_text": "ブロックB",
            "sentence": "Bの文",
            "route": "diagram",
            "filename": "3.png",
            "status": "ok",
        },
    ]
    (job_dir / "manifest.json").write_text(
        json.dumps({"title": "テスト", "rows": rows}, ensure_ascii=False),
        encoding="utf-8",
    )

    client = appmod.app.test_client()
    resp = client.get("/download/block/job_block/1/0")

    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        names = set(zf.namelist())
        assert "images/1.png" in names
        assert "images/2.png" in names
        assert "images/3.png" not in names
        assert "block.csv" in names
        assert "block_manifest.json" in names
        manifest = json.loads(zf.read("block_manifest.json").decode("utf-8"))
        assert manifest["block_order_key"] == "ch01_block001"
        assert manifest["sentence_nos"] == [1, 2]

