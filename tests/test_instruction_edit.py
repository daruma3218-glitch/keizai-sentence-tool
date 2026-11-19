"""素材画面の「手直し」は、編集者の依頼だけを元画像に当て、ほかは変えない（2026-11-19）。

さゆみさん（11/18）「画像を作り直す際、指示していない部分まで勝手に変更されることが多い印象でした
（図解スタジオと比較して）。その都度『それ以外は変更禁止』と指示して対応していました」。
手直しは「場面の最初の指示（図の決まり・構図の案 Variant A/B…・文字の決まり）＋依頼」に
「読みやすさ・重なりも直す」を付けて送っていたため、描き直しになっていた。2枚頼むと2枚目は別の構図を指示していた。
図解スタジオ（zukai-tool の image_edit.INSTRUCT_SUFFIX）と同じ「依頼＋ほかは全く同じに保つ」にする。

- 先生のいるチャンネル（世界観ロック＋見本）では、元画像を1枚目・先生の見本を後ろに渡す 11/15 の決まりは保つ
- 場面の指示・構図の案・チャンネルの画風の文・文字の決まりは送らない
- 元画像が無い時・新しい候補を作る時は今までどおり
"""
import asyncio
import json
from pathlib import Path

from PIL import Image

import generator
import suasi_presenter as presenter

ROOT = Path(__file__).resolve().parents[1]
PRIMARY = ROOT / "references" / "keizai_professor.png"
WORLD = next(c for c in json.loads((ROOT / "channels.json").read_text(encoding="utf-8"))["channels"]
             if c["id"] == "keizai")["defaults"]["worldview_desc"]
SCENE = ("Create a clear educational diagram.\nVariant B: relationship map, center-out, main subject in the "
         "center with 3 connected nodes.\nFix mode: Use the attached reference image as the style/composition "
         "anchor.\nSource sentence: 金利が上がると円高になります。\nEditor request: 矢印を赤にして")
ASK = "矢印を赤にして"


def _run(tmp_path, monkeypatch, entry, *, style_lock=WORLD, reference=PRIMARY, edit=True):
    source = tmp_path / "source.png"
    Image.new("RGB", (32, 18), (177, 214, 238)).save(source)
    gen = generator.ParallelImageGenerator(
        provider=generator.PROVIDER_GPT_IMAGE, openai_api_key="test-only",
        style_preset="flat_infographic", concurrency=1,
        reference_image_path=str(reference) if reference else None,
        edit_image_path=str(source) if edit else None, style_lock_text=style_lock)
    seen = []

    def fake_dispatch(full_prompt, output_path, use_reference=False, ref_bytes_override=None, ref_mime_override=None):
        seen.append((full_prompt, ref_bytes_override))
        Image.new("RGB", (32, 18), (220, 225, 232)).save(output_path)
        return True, ""

    monkeypatch.setattr(gen, "_dispatch_sync_generate", fake_dispatch)
    (tmp_path / "out").mkdir(exist_ok=True)
    assert asyncio.run(gen.generate_all([entry], tmp_path / "out"))[0]["success"]
    return seen[0][0], seen[0][1], source.read_bytes()


def _entry(**extra):
    return {"index": 1, "prompt": SCENE, "type": "diagram", "edit_source": True,
            "edit_mode": "instruction_only", "edit_instruction": ASK,
            "chapter_index": 0, "block_index": 2, "allowed_terms": ["金利", "円高"], **extra}


def _assert_only_the_request(prompt):
    assert generator.instruction_edit_prompt(ASK) in prompt
    assert "Only change what is requested above" in prompt
    # 描き直しの元だった指示を送らない
    for redraw in ("Variant B", "Fix mode", "Source sentence", "Create a clear educational diagram",
                   "CHANNEL ART STYLE", "TEXT POLICY", "COMPOSITION (CRITICAL)",
                   "Use at most 6 short labels", generator._EDIT_SOURCE_INSTRUCTION):
        assert redraw not in prompt, redraw


def test_professor_channel_sends_only_the_request_with_the_professor_samples(tmp_path, monkeypatch):
    prompt, refs, source = _run(tmp_path, monkeypatch, _entry())
    _assert_only_the_request(prompt)
    # 先生の顔がずれないように、元画像を1枚目・見本を後ろに渡す決まり（11/15）は保つ
    assert prompt.startswith(generator._EDIT_CHARACTER_REFERENCE_INSTRUCTION)
    assert isinstance(refs, list) and len(refs) >= 2
    assert refs[0][0] == source


def test_channel_without_lock_sends_exactly_the_request_and_the_source(tmp_path, monkeypatch):
    prompt, refs, source = _run(tmp_path, monkeypatch, _entry(), style_lock="", reference=None)
    assert prompt == generator.instruction_edit_prompt(ASK)
    assert refs == source


def test_character_scene_without_lock_does_not_describe_a_missing_reference(tmp_path, monkeypatch):
    prompt, refs, source = _run(tmp_path, monkeypatch, _entry(type="illustration", character=True),
                                style_lock="")
    assert prompt == generator.instruction_edit_prompt(ASK)
    assert refs == source


def test_suasi_board_rule_is_kept_around_the_request(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    Image.new("RGB", (1024, 576), "white").save(source)
    presenter.compose_presenter(source, presenter.load_artwork("thinking"), "thinking", "fixture")
    gen = generator.ParallelImageGenerator(provider="gpt-image", openai_api_key="test-only",
                                           style_lock_text="[OTONA_SUASI_B_V1]", edit_image_path=str(source))
    seen = []

    def dispatch(prompt, path, *args):
        seen.append(prompt)
        Image.new("RGB", (1024, 576), "white").save(path)
        return True, ""

    monkeypatch.setattr(gen, "_dispatch_sync_generate", dispatch)
    result = asyncio.run(gen.generate_all([_entry()], tmp_path / "edited"))
    assert result[0]["success"] and result[0]["presenter"]["pose_id"] == "thinking"
    assert seen[0].startswith(presenter.BOARD_INSTRUCTION) and seen[0].endswith(presenter.BOARD_INSTRUCTION)
    assert generator.instruction_edit_prompt(ASK) in seen[0]
    assert "Variant B" not in seen[0]


def test_without_source_image_the_scene_is_generated_as_before(tmp_path, monkeypatch):
    prompt, _, _ = _run(tmp_path, monkeypatch, _entry(), edit=False)
    assert "Variant B" in prompt and "CHANNEL ART STYLE" in prompt
    assert generator.instruction_edit_prompt(ASK) not in prompt


def test_old_edit_without_mode_keeps_the_previous_refine_prompt(tmp_path, monkeypatch):
    entry = _entry()
    entry.pop("edit_mode")
    prompt, _, _ = _run(tmp_path, monkeypatch, entry)
    assert prompt.startswith(generator._EDIT_SOURCE_INSTRUCTION)
    assert "Variant B" in prompt


def test_empty_request_never_becomes_a_blank_edit(tmp_path, monkeypatch):
    prompt, _, _ = _run(tmp_path, monkeypatch, _entry(edit_instruction="  "))
    assert "REQUESTED CHANGE" not in prompt
