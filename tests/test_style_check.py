#!/usr/bin/env python3
"""世界観ロックの画風チェック（style_check）の pytest。

受け入れ基準:
- 判定役には基準画像（1枚目）と判定画像（2枚目）をこの順で渡し、5段階の style_score を読む。
  合格線（3点以下は作り直し）はコードが決め、読めない点数では作り直さない
- 画風チェックはイラストだけ（図解・実写は対象外）
- 画風が外れたイラストだけを 1 回作り直し、もう一度だけ判定する（意味のズレだけでは作り直さない）
- 作り直しは最初と同じ画像モデル・世界観ロック・先生の参照画像で行う
- 作り直しで点数が上がらなければ元の画像に戻す／失敗しても元の画像を残す／1ジョブの上限がある
- 図解の作り直し（_verify_and_fix）も最初と同じ画像モデルで行う
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

import app as appmod  # noqa: E402
import pipeline as plmod  # noqa: E402
import verifier  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "references" / "keizai_professor.png"
LOCK = "ART STYLE: flat cartoon with bold outlines."


class _FakeClient:
    def with_options(self, **kw):
        return self


def _png(path, color=(220, 225, 232)):
    # verify_image は 200 バイト未満を「空の画像」として判定しないため、模様のある画像にする
    base = Image.new("RGB", (160, 90), color)
    base.paste(Image.radial_gradient("L").resize((90, 90)).convert("RGB"), (35, 0))
    base.save(path)


def _pipe(tmp_path, **kw):
    base = dict(manuscript_text="x" * 200, output_dir=tmp_path / "job", channel_id="keizai",
                worldview_desc=LOCK, style_lock=True, style_check=True, character_ref_path=str(REF),
                provider="nanobanana", type_providers={"diagram": "gpt-image"})
    base.update(kw)
    return plmod.SentencePipeline(**base)


def test_keizai_channel_turns_on_style_check():
    assert appmod.get_channel("keizai")["defaults"]["style_check"] is True


def test_style_check_needs_lock_and_reference(tmp_path):
    assert _pipe(tmp_path).style_check is True
    assert _pipe(tmp_path, character_ref_path="").style_check is False
    assert _pipe(tmp_path, style_lock=False).style_check is False
    assert _pipe(tmp_path, worldview_desc="").style_check is False


def test_verifier_sends_reference_first_and_reads_style_score(tmp_path):
    img = tmp_path / "7.png"
    _png(img, (10, 20, 30))
    sent = {}

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                sent.update(kw)
                return SimpleNamespace(content=[SimpleNamespace(
                    text='{"ok": true, "reason": "伝わる", "issue_tags": [], "fix_hint": "", '
                         '"style_score": 2, "style_issue": "つやのある塗り"}')])

    v = verifier.verify_image(Client, img, "文", img_type="illustration",
                              style_reference_path=REF, style_rules=LOCK)
    content = sent["messages"][0]["content"]
    assert [c["type"] for c in content] == ["image", "image", "text"]
    ref_b64, _ = verifier._encode_for_review(REF.read_bytes(), ".png", max_side=640)
    assert content[0]["source"]["data"] == ref_b64  # 1枚目＝基準画像
    assert content[1]["source"]["data"] != ref_b64  # 2枚目＝判定画像
    assert "1枚目はチャンネルの基準画像" in content[2]["text"] and LOCK in content[2]["text"]
    assert v["ok"] is True and v["style_score"] == 2 and v["style_ok"] is False
    assert v["style_issue"] == "つやのある塗り"


def test_verifier_without_style_reference_is_unchanged(tmp_path):
    img = tmp_path / "7.png"
    _png(img)
    sent = {}

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                sent.update(kw)
                return SimpleNamespace(content=[SimpleNamespace(text='{"ok": true, "reason": "OK", "fix_hint": ""}')])

    v = verifier.verify_image(Client, img, "文")
    assert [c["type"] for c in sent["messages"][0]["content"]] == ["image", "text"]
    assert "画風チェック" not in sent["messages"][0]["content"][1]["text"]
    assert "style_ok" not in v and "style_score" not in v


def _style_verdict(tmp_path, raw_score):
    img = tmp_path / "7.png"
    _png(img)
    text = '{"ok": true, "reason": "OK", "style_score": %s}' % raw_score

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                return SimpleNamespace(content=[SimpleNamespace(text=text)])

    return verifier.verify_image(Client, img, "文", style_reference_path=REF, style_rules=LOCK)


def test_style_pass_line_is_decided_by_code(tmp_path):
    # 3点以下（少し浮く〜実写）は作り直し。4点（細部だけ違う）以上はそのまま
    assert verifier.STYLE_SCORE_REGENERATE_MAX == 3
    assert _style_verdict(tmp_path, 1)["style_ok"] is False
    assert _style_verdict(tmp_path, 3)["style_ok"] is False
    assert _style_verdict(tmp_path, 4)["style_ok"] is True
    assert _style_verdict(tmp_path, 5)["style_ok"] is True


def test_unreadable_style_score_is_not_a_failure(tmp_path):
    for raw in ('"たぶん"', "7", "0", "null", "2.5"):
        v = _style_verdict(tmp_path, raw)
        assert v["style_ok"] is None and v["style_score"] is None, raw  # 読めない判定で作り直さない


def _setup_flag_check(tmp_path, monkeypatch, verdicts, gen_ok=True, extra_targets=()):
    """verdicts: {(no, pass_no): verdict}。pass_no=1 が最初の判定、2 が作り直し後。"""
    monkeypatch.setattr(plmod, "get_anthropic_client", lambda key="": _FakeClient())
    seen_verify = []
    counts = {}

    def fake_verify(client, image_path, sentence, img_type="diagram", **kw):
        no = int(Path(image_path).stem)
        counts[no] = counts.get(no, 0) + 1
        seen_verify.append((no, kw.get("style_reference_path"), kw.get("style_rules")))
        return verdicts.get((no, counts[no]),
                            {"ok": True, "verified": True, "reason": "", "style_ok": True, "style_score": 5})

    monkeypatch.setattr(verifier, "verify_image", fake_verify)
    gen_calls = []

    def fake_generate(prompts, output_dir, **kw):
        gen_calls.append((prompts, kw))
        out = []
        for p in prompts:
            if gen_ok:
                _png(Path(output_dir) / f"{p['index']}.png", (1, 2, 3))
                kw["progress_callback"]({"index": p["index"], "status": "ok", "filename": f"{p['index']}.png"})
            else:
                kw["progress_callback"]({"index": p["index"], "status": "failed", "error": "boom"})
            out.append({"index": p["index"], "success": gen_ok, "filename": f"{p['index']}.png" if gen_ok else None})
        return out

    monkeypatch.setattr(plmod, "run_parallel_generation", fake_generate)
    pipe = _pipe(tmp_path)
    targets = [
        {"index": 1, "type": "illustration", "prompt": "A shop.", "excerpt": "一", "allowed_terms": [], "character": False},
        {"index": 2, "type": "illustration", "prompt": "The professor.", "excerpt": "二", "allowed_terms": [], "character": True},
        {"index": 3, "type": "illustration", "prompt": "A cafe.", "excerpt": "三", "allowed_terms": []},
        *extra_targets,
    ]
    for t in targets:
        no = t["index"]
        _png(pipe.images_dir / f"{no}.png")
        pipe._rows_state[no] = {"no": no, "status": "ok", "filename": f"{no}.png"}
    results = [{"success": True, "filename": f"{t['index']}.png", "index": t["index"]} for t in targets]
    return pipe, results, targets, gen_calls, seen_verify


def _bad(score, issue="厚塗り"):
    return {"ok": True, "verified": True, "reason": "", "style_score": score, "style_issue": issue,
            "style_ok": score > verifier.STYLE_SCORE_REGENERATE_MAX}


def test_only_style_failures_are_regenerated_once_then_rechecked(tmp_path, monkeypatch):
    verdicts = {
        (1, 1): _bad(2, "厚塗り"),
        (2, 1): _bad(2, "別人の先生"),
        (3, 1): {"ok": False, "verified": True, "reason": "文とズレ", "style_ok": True, "style_score": 5},  # 意味だけ
        (1, 2): _bad(5, ""),
        (2, 2): _bad(3, "まだ少し浮く"),  # 良くなったが合格線未満 → 新しい方を残して⚠
    }
    pipe, results, targets, gen_calls, seen_verify = _setup_flag_check(tmp_path, monkeypatch, verdicts)
    pipe._flag_check_ai_images(results, targets, theme="T", keys=("g", "o"))

    # 判定には毎回、基準画像と世界観の設定文を渡す
    assert all(ref == str(REF) and rules == LOCK for _, ref, rules in seen_verify)
    # 作り直しは画風が外れた 1・2 だけ、1回だけ
    assert len(gen_calls) == 1
    prompts, kw = gen_calls[0]
    assert sorted(p["index"] for p in prompts) == [1, 2]
    assert all("Redraw this scene strictly in the CHANNEL ART STYLE" in p["prompt"] for p in prompts)
    assert kw["provider"] == "nanobanana"  # illustration は主プロバイダ（最初と同じ）
    assert kw["style_lock_text"] == LOCK and kw["reference_image_path"] == str(REF)
    assert next(p for p in prompts if p["index"] == 2)["character"] is True  # 先生は参照画像つき
    rows = pipe._rows_state
    assert rows[1]["verify_status"] == "pass" and rows[1]["style_fixed"] is True
    assert rows[2]["verify_issue"] is True and "作り直し後も要確認" in rows[2]["verify_reason"]
    assert rows[3]["verify_status"] == "needs_fix" and "文とズレ" in rows[3]["verify_reason"]
    assert "画風" not in rows[3]["verify_reason"]
    # 作り直す前の画像は images/ の外に退避（ZIPや一覧に混ぜない）
    assert (pipe.output_dir / "style_before" / "1.png").exists()


def test_worse_regeneration_is_rolled_back(tmp_path, monkeypatch):
    verdicts = {(1, 1): _bad(3, "つやのある塗り"), (1, 2): _bad(2, "もっと厚塗り")}
    pipe, results, targets, gen_calls, _ = _setup_flag_check(tmp_path, monkeypatch, verdicts)
    original = (pipe.images_dir / "1.png").read_bytes()
    pipe._flag_check_ai_images(results, targets, theme="T", keys=("g", "o"))
    assert len(gen_calls) == 1
    assert (pipe.images_dir / "1.png").read_bytes() == original  # 作り直しで悪くしない
    row = pipe._rows_state[1]
    assert row["verify_issue"] is True and "元の画像のまま" in row["verify_reason"]
    assert "3/5点" in row["verify_reason"]


def test_failed_regeneration_keeps_the_original_image(tmp_path, monkeypatch):
    verdicts = {(1, 1): _bad(1, "実写風")}
    pipe, results, targets, gen_calls, _ = _setup_flag_check(tmp_path, monkeypatch, verdicts, gen_ok=False)
    original = (pipe.images_dir / "1.png").read_bytes()
    pipe._flag_check_ai_images(results, targets, theme="T", keys=("g", "o"))
    row = pipe._rows_state[1]
    assert row["status"] == "ok" and row["filename"] == "1.png"
    assert "元の画像のまま" in row["verify_reason"]
    assert (pipe.images_dir / "1.png").read_bytes() == original


def test_style_regeneration_is_capped_and_worst_first(tmp_path, monkeypatch):
    verdicts = {(1, 1): _bad(3), (2, 1): _bad(1), (3, 1): _bad(2)}
    pipe, results, targets, gen_calls, _ = _setup_flag_check(tmp_path, monkeypatch, verdicts)
    monkeypatch.setattr(plmod.SentencePipeline, "STYLE_FIX_MAX", 2)
    pipe._flag_check_ai_images(results, targets, theme="T", keys=("g", "o"))
    assert sorted(p["index"] for prompts, _ in gen_calls for p in prompts) == [2, 3]  # 点数の低い順


def test_no_regeneration_without_keys(tmp_path, monkeypatch):
    verdicts = {(1, 1): _bad(2)}
    pipe, results, targets, gen_calls, _ = _setup_flag_check(tmp_path, monkeypatch, verdicts)
    pipe._flag_check_ai_images(results, targets, theme="T")  # keys なし（従来の呼び出し）
    assert gen_calls == []
    assert pipe._rows_state[1]["verify_issue"] is True and "画風（2/5点）" in pipe._rows_state[1]["verify_reason"]


def test_style_check_is_only_for_illustrations(tmp_path, monkeypatch):
    photo = {"index": 4, "type": "realphoto", "prompt": "A street.", "excerpt": "四", "allowed_terms": []}
    pipe, results, targets, gen_calls, seen_verify = _setup_flag_check(
        tmp_path, monkeypatch, {}, extra_targets=(photo,))
    pipe._flag_check_ai_images(results, targets, theme="T", keys=("g", "o"))
    refs = {no: ref for no, ref, _ in seen_verify}
    assert refs[1] == str(REF) and refs[4] is None  # 実写は画風チェックしない（作り直しても実写のまま）
    assert gen_calls == []


def test_diagram_fix_uses_the_same_provider_and_no_style_check(tmp_path, monkeypatch):
    monkeypatch.setattr(plmod, "get_anthropic_client", lambda key="": _FakeClient())
    seen = []

    def fake_verify(*a, **kw):
        seen.append(kw)
        return {"ok": False, "verified": True, "reason": "矢印が逆", "fix_hint": "Reverse the arrow.", "issue_tags": []}

    monkeypatch.setattr(verifier, "verify_image", fake_verify)
    calls = []
    monkeypatch.setattr(plmod, "run_parallel_generation", lambda **kw: calls.append(kw) or [])
    pipe = _pipe(tmp_path)
    _png(pipe.images_dir / "5.png")
    pipe._rows_state[5] = {"no": 5, "status": "ok"}
    target = {"index": 5, "type": "diagram", "prompt": "Cup -> shop.", "excerpt": "五", "allowed_terms": []}
    pipe._verify_and_fix([{"success": True, "index": 5}], [target], "g", "o", theme="T")
    assert "style_reference_path" not in seen[0]  # 図解は人物の基準画像と比べない
    assert len(calls) == 1
    assert calls[0]["provider"] == "gpt-image"  # type_providers の図解モデルで作り直す
    assert calls[0]["style_lock_text"] == LOCK
    assert "IMPROVE: Reverse the arrow." in calls[0]["prompts"][0]["prompt"]


def test_worker_blink_is_retried_once(tmp_path, monkeypatch):
    """PCワーカーの生存確認が一瞬途切れた（worker_unavailable）判定は、20秒後に1回だけやり直す。"""
    import time
    import subscription_runtime
    monkeypatch.setattr(time, "sleep", lambda s: None)
    pipe, results, targets, gen_calls, seen_verify = _setup_flag_check(tmp_path, monkeypatch, {})
    real_verify = verifier.verify_image
    blinked = []

    def flaky(client, image_path, *a, **kw):
        no = int(Path(image_path).stem)
        if no == 1 and not blinked:
            blinked.append(no)
            raise subscription_runtime.SubscriptionUnavailable("worker_unavailable", request_id="x")
        if no == 2:
            raise subscription_runtime.SubscriptionUnavailable("both_cli_unavailable", request_id="y")
        return real_verify(client, image_path, *a, **kw)

    monkeypatch.setattr(verifier, "verify_image", flaky)
    pipe._flag_check_ai_images(results, targets, theme="T", keys=("g", "o"))
    assert blinked == [1]
    assert pipe._rows_state[1]["verify_status"] == "pass"       # やり直しで判定できた
    assert pipe._rows_state[2]["verify_status"] == "unverified"  # 別の理由の失敗はやり直さない
