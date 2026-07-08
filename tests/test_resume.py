#!/usr/bin/env python3
"""ジョブ途中再開（チェックポイント）の pytest。

受け入れ基準:
- _build_resume_args: job.json のパラメータを復元し、無いもの（旧ジョブ）は
  チャンネル既定値で補完。完了済み／原稿なしは再開不可のエラーを返す
- pipeline(resume=True): split/routes/prompts をディスクから再利用（Claude呼び出しゼロ）、
  生成済み画像はスキップして残りだけ生成。manifest の generated は再利用分を含む
- routes.json の文字列キー（JSON化の副作用）を int に正規化して使える
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as appmod  # noqa: E402
import pipeline as plmod  # noqa: E402


# ---------- _build_resume_args ----------

def _make_job(tmp_path, job_id="20260708_000000", with_manifest=False, manuscript=True, state=None):
    job_dir = tmp_path / job_id
    (job_dir / "images").mkdir(parents=True)
    if manuscript:
        (job_dir / "manuscript.txt").write_text("こ" * 200, encoding="utf-8")
    if with_manifest:
        (job_dir / "manifest.json").write_text("{}", encoding="utf-8")
    if state is not None:
        (job_dir / "job.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return job_dir


def test_build_resume_args_restores_and_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path)
    # 旧ジョブ想定: 一部パラメータのみ保存（route_mode 等は未保存 → チャンネル既定で補完）
    _make_job(tmp_path, state={
        "channel_id": "keizai", "provider": "nanobanana",
        "concurrency": 99,  # 上限クランプされるはず
        "style_preset": "flat_infographic",
    })
    args, err = appmod._build_resume_args("20260708_000000")
    assert err is None
    (job_id, manuscript_text, user_instructions, concurrency, provider, openai_quality,
     skip_decorative, style_preset, web_image_count, max_diagrams, route_mode,
     worldview_desc, verify_diagrams, channel_id, ch_keys, character_ref_path,
     title_override, fact_context, resume) = args
    assert resume is True
    assert channel_id == "keizai" and provider == "nanobanana"
    assert concurrency == 24  # クランプ
    assert route_mode in ("auto", "all_ai")
    assert isinstance(worldview_desc, str) and worldview_desc  # keizai既定で補完
    assert isinstance(verify_diagrams, bool)
    assert manuscript_text.startswith("こ")


def test_build_resume_args_rejects_completed_and_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path)
    _make_job(tmp_path, job_id="done_job", with_manifest=True, state={})
    args, err = appmod._build_resume_args("done_job")
    assert args is None and "完了済み" in err

    _make_job(tmp_path, job_id="no_manuscript", manuscript=False, state={})
    args, err = appmod._build_resume_args("no_manuscript")
    assert args is None and "原稿" in err

    args, err = appmod._build_resume_args("not_exist")
    assert args is None


# ---------- pipeline resume（成果物の再利用と生成スキップ） ----------

def test_pipeline_resume_skips_done_images(tmp_path, monkeypatch):
    job_dir = tmp_path / "job"
    (job_dir / "images").mkdir(parents=True)

    rows = [
        {"no": 1, "sentence": "一文目のテスト文章です。", "chapter_title": "第1章", "block_text": ""},
        {"no": 2, "sentence": "二文目のテスト文章です。", "chapter_title": "第1章", "block_text": ""},
        {"no": 3, "sentence": "三文目のテスト文章です。", "chapter_title": "第1章", "block_text": ""},
    ]
    (job_dir / "split_result.json").write_text(json.dumps({
        "analysis": {"title": "再開テスト"},
        "chapters": [{"title": "第1章", "blocks": []}],
        "rows": rows,
        "total_sentences": 3,
    }, ensure_ascii=False), encoding="utf-8")
    # JSON往復で文字列キーになった routes（正規化されること）
    (job_dir / "routes.json").write_text(json.dumps({
        "1": {"route": "illustration", "reason": "r"},
        "2": {"route": "illustration", "reason": "r"},
        "3": {"route": "illustration", "reason": "r"},
    }), encoding="utf-8")
    (job_dir / "prompts.json").write_text(json.dumps({"rows": [
        {**r, "prompt": "an illustration", "type": "illustration", "allowed_terms": []}
        for r in rows
    ]}, ensure_ascii=False), encoding="utf-8")
    # №2 は前回生成済み（1KB超）
    (job_dir / "images" / "2.png").write_bytes(b"\x89PNG" + b"0" * 2000)

    # 再開なら Claude 系は一切呼ばれない（呼ばれたら失敗）
    def boom(*a, **k):
        raise AssertionError("resume中に呼んではいけない")
    monkeypatch.setattr(plmod, "get_anthropic_client", lambda key="": object())
    monkeypatch.setattr(plmod, "split_manuscript", boom)
    monkeypatch.setattr(plmod, "route_all_sentences", boom)
    monkeypatch.setattr(plmod, "generate_all_prompts", boom)

    captured = {}

    def fake_generate(**kw):
        captured["targets"] = [t["index"] for t in kw.get("prompts", [])]
        return [{"index": t["index"], "filename": f"{t['index']}.png", "success": True,
                 "type": t.get("type"), "prompt": t.get("prompt"), "error": ""}
                for t in kw.get("prompts", [])]
    monkeypatch.setattr(plmod, "run_parallel_generation", fake_generate)

    pipe = plmod.SentencePipeline(
        manuscript_text="て" * 200, output_dir=job_dir,
        provider="nanobanana", gemini_key="dummy",
        verify_diagrams=False, web_image_count=0, route_mode="auto",
        resume=True,
    )
    manifest = pipe.run()

    assert captured["targets"] == [1, 3], "生成済み№2はスキップされるべき"
    assert manifest.get("generated") == 3, "再利用分を含めて3枚が完成扱い"
    snap = json.loads((job_dir / "rows_progress.json").read_text(encoding="utf-8"))
    row2 = next(r for r in snap["rows"] if r["no"] == 2)
    assert row2["status"] == "ok" and row2["filename"] == "2.png"
