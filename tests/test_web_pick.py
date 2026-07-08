#!/usr/bin/env python3
"""Web写真の候補選択（改修③）の pytest。

受け入れ基準:
- /api/web_candidates: 保存済み候補ページの画像を解決して返す（候補順）
- /api/web_pick: 選んだ画像をDLして images/{no}.jpg に差し替え、snapshotの出典を更新
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as appmod  # noqa: E402
import web_searcher  # noqa: E402


def _client_logged_in():
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()
    with c.session_transaction() as s:
        s["authenticated"] = True
    return c


def _make_job(tmp_path, job_id="20260708_010101"):
    job_dir = tmp_path / job_id
    (job_dir / "images").mkdir(parents=True)
    (job_dir / "rows_progress.json").write_text(json.dumps({"rows": [{
        "no": 7, "status": "ok", "filename": "7.jpg",
        "web_source_url": "https://ja.wikipedia.org/wiki/A",
        "web_candidates": [
            {"url": "https://ja.wikipedia.org/wiki/A", "title": "A記事"},
            {"url": "https://example.com/news/b", "title": "B記事"},
            {"url": "https://example.com/no-image", "title": "C記事"},
        ],
    }]}, ensure_ascii=False), encoding="utf-8")
    return job_dir


def test_web_candidates_resolves_images(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path)
    _make_job(tmp_path)
    monkeypatch.setattr(web_searcher, "_wikipedia_image_url", lambda u: "https://img/wiki_a.jpg")
    monkeypatch.setattr(web_searcher, "_page_main_image",
                        lambda u, timeout=8: "https://img/og_b.jpg" if "news/b" in u else "")
    c = _client_logged_in()
    res = c.get("/api/web_candidates/20260708_010101/7")
    assert res.status_code == 200
    data = res.get_json()
    urls = [it["image_url"] for it in data["items"]]
    assert urls == ["https://img/wiki_a.jpg", "https://img/og_b.jpg"], "候補順・画像なしは除外"
    assert data["current"] == "https://ja.wikipedia.org/wiki/A"


def test_web_pick_replaces_image_and_source(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path)
    job_dir = _make_job(tmp_path)

    def fake_download(url, out):
        Path(out).write_bytes(b"\xff\xd8JPEG" + b"0" * 2000)
        return True
    monkeypatch.setattr(web_searcher, "download_thumbnail", fake_download)

    c = _client_logged_in()
    res = c.post("/api/web_pick/20260708_010101/7", data={
        "page_url": "https://example.com/news/b",
        "image_url": "https://img/og_b.jpg",
        "title": "B記事",
    })
    assert res.status_code == 200 and res.get_json()["ok"] is True
    assert (job_dir / "images" / "7.jpg").exists()
    snap = json.loads((job_dir / "rows_progress.json").read_text(encoding="utf-8"))
    row = snap["rows"][0]
    assert row["web_source_url"] == "https://example.com/news/b"
    assert row["web_local_file"] == "7.jpg" and row["web_picked"] is True
    assert row["status"] == "ok" and row["filename"] == "7.jpg"


def test_web_pick_rejects_unresolvable(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path)
    _make_job(tmp_path)
    monkeypatch.setattr(web_searcher, "_page_main_image", lambda u, timeout=8: "")
    c = _client_logged_in()
    res = c.post("/api/web_pick/20260708_010101/7", data={"page_url": "https://example.com/no-image"})
    assert res.status_code == 422
