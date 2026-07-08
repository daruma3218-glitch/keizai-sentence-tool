#!/usr/bin/env python3
"""イラスト/実写風の軽量検品フラグ（改修②）の pytest。

受け入れ基準:
- illustration/realphoto の生成成功分だけ検品し、NG に verify_issue/reason を立てる
- diagram/chart は対象外（既存の _verify_and_fix が担当）
- 自動再生成はしない（run_parallel_generation を呼ばない）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline as plmod  # noqa: E402
import verifier  # noqa: E402


class _FakeClient:
    def with_options(self, **kw):
        return self


def test_flag_check_marks_only_bad_ai_images(tmp_path, monkeypatch):
    monkeypatch.setattr(plmod, "get_anthropic_client", lambda key="": _FakeClient())

    calls = []

    def fake_verify(client, image_path, sentence, img_type="diagram", **kw):
        calls.append((Path(image_path).name, img_type))
        # №1 はNG（被写体ズレ）、№2 はOK
        if "1.png" in str(image_path):
            return {"ok": False, "reason": "被写体が文とズレ", "fix_hint": "", "issue_tags": ["mismatch"]}
        return {"ok": True, "reason": "", "fix_hint": ""}
    monkeypatch.setattr(verifier, "verify_image", fake_verify)

    def boom(**kw):
        raise AssertionError("フラグ検品で再生成してはいけない")
    monkeypatch.setattr(plmod, "run_parallel_generation", boom)

    pipe = plmod.SentencePipeline(manuscript_text="x" * 200, output_dir=tmp_path / "job")
    results = [
        {"success": True, "filename": "1.png", "index": 1},
        {"success": True, "filename": "2.png", "index": 2},
        {"success": True, "filename": "3.png", "index": 3},   # diagram → 対象外
        {"success": False, "filename": None, "index": 4},      # 失敗 → 対象外
    ]
    targets = [
        {"index": 1, "type": "illustration", "excerpt": "一", "allowed_terms": [], "block_text": "", "section": ""},
        {"index": 2, "type": "realphoto", "excerpt": "二", "allowed_terms": [], "block_text": "", "section": ""},
        {"index": 3, "type": "diagram", "excerpt": "三", "allowed_terms": [], "block_text": "", "section": ""},
        {"index": 4, "type": "illustration", "excerpt": "四", "allowed_terms": [], "block_text": "", "section": ""},
    ]
    pipe._flag_check_ai_images(results, targets, theme="T")

    checked = {name for name, _ in calls}
    assert checked == {"1.png", "2.png"}, "illustration/realphoto の成功分のみ検品"
    assert pipe._rows_state.get(1, {}).get("verify_issue") is True
    assert "ズレ" in pipe._rows_state.get(1, {}).get("verify_reason", "")
    assert not pipe._rows_state.get(2, {}).get("verify_issue")
