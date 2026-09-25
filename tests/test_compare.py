#!/usr/bin/env python3
"""版を比べる画面（/compare）の pytest。

受け入れ基準:
- 選んだ順にジョブを列に並べ、1列目の文の順に同じ文（空白・全角記号の違いは無視）の画像を横に並べる
- 2文が1文にまとまった版も、含み合う文で並べる／無い文は「同じ文がありません」
- 列の見出しに画像モデルと世界観ロックの有無（古いジョブは「旧設定」）を出す
- 不正なジョブ名は無視する（読み取り専用・ディレクトリ外を読まない）
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as appmod  # noqa: E402


def _job(root, job_id, rows, **manifest):
    d = root / job_id
    (d / "images").mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"title": job_id, "rows": rows, **manifest}, ensure_ascii=False), encoding="utf-8")
    (d / "job.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")


def _client(monkeypatch, root):
    monkeypatch.setattr(appmod, "OUTPUT_DIR", root)
    monkeypatch.setattr(appmod, "APP_PASSWORD", "")
    return appmod.app.test_client()


def test_compare_lines_up_the_same_sentences(tmp_path, monkeypatch):
    root = tmp_path / "output"
    _job(root, "20260723_000000", [
        {"no": 1, "sentence": "あなたなら、いくらまで出しますか?", "filename": "1.png", "route": "illustration"},
        {"no": 2, "sentence": "中身はまったく同じなのに。", "filename": "2.png", "route": "illustration"},
        {"no": 3, "sentence": "寂れた雑貨店です。", "filename": "", "route": "web_photo", "web_local_file": "web_3.jpg"},
    ], provider="gpt-image")
    _job(root, "20260925_000001", [
        {"no": 5, "sentence": "あなたなら、いくらまで出しますか？研究では答えが変わりました。", "filename": "5.png",
         "route": "illustration", "style_fixed": True},
        {"no": 6, "sentence": "中身は まったく同じなのに。", "filename": "6.png", "route": "illustration",
         "verify_issue": True, "verify_reason": "画風（3/5点）: つやのある塗り"},
    ], provider="gpt-image", openai_model="gpt-image-2.5-flare", style_lock=True)
    html = _client(monkeypatch, root).get("/compare?jobs=20260723_000000,20260925_000001,../etc").get_data(as_text=True)
    assert "すべて gpt-image｜旧設定（世界観ロック前）" in html
    assert "すべて gpt-image-2.5-flare｜世界観ロック" in html
    # 含み合う文（2文が1文になった版）と、空白の違いだけの文が並ぶ
    assert "/results/20260925_000001/images/5.png" in html and "/results/20260925_000001/images/6.png" in html
    assert "/results/20260723_000000/images/web_3.jpg" in html
    assert "画風を作り直し済み" in html and "⚠ 画風（3/5点）: つやのある塗り" in html
    assert "この版には同じ文がありません" in html  # 3番目の文は新しい版に無い
    assert "../etc" not in html.split("<table", 1)[1]


def test_compare_without_jobs_shows_the_picker(tmp_path, monkeypatch):
    root = tmp_path / "output"
    _job(root, "20260925_000001", [{"no": 1, "sentence": "文。", "filename": "1.png"}], channel_id="keizai")
    (root / "scene_fix_20260925_000002_abcdef").mkdir(parents=True)
    html = _client(monkeypatch, root).get("/compare").get_data(as_text=True)
    assert 'value="20260925_000001"' in html and "scene_fix_" not in html
    assert "<table" not in html
