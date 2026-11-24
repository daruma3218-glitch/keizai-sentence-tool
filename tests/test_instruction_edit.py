"""素材画面の「手直し」は、編集者の依頼だけを元画像に当て、ほかは変えない（2026-11-19）。

さゆみさん（11/18）「画像を作り直す際、指示していない部分まで勝手に変更されることが多い印象でした
（図解スタジオと比較して）。その都度『それ以外は変更禁止』と指示して対応していました」。
手直しは「場面の最初の指示（図の決まり・構図の案 Variant A/B…・文字の決まり）＋依頼」に
「読みやすさ・重なりも直す」を付けて送っていたため、描き直しになっていた。2枚頼むと2枚目は別の構図を指示していた。
図解スタジオ（zukai-tool の image_edit.INSTRUCT_SUFFIX）と同じ「依頼＋ほかは全く同じに保つ」にする。

- カラクリの先生本人の修正には見本を渡し、一般の人物・図解の手直しには元画像だけを渡す
- 場面の指示・構図の案・チャンネルの画風の文・文字の決まりは送らない
- 元画像が無い時・新しい候補を作る時は今までどおり
"""
import asyncio
import json
from pathlib import Path

from PIL import Image

import generator
import pytest
from character_refs import edit_subject
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


def test_arrow_edit_in_professor_channel_uses_source_only(tmp_path, monkeypatch):
    prompt, refs, source = _run(tmp_path, monkeypatch, _entry())
    _assert_only_the_request(prompt)
    assert refs == source and "professor" not in prompt


def test_editing_the_professor_keeps_the_four_authenticated_samples(tmp_path, monkeypatch):
    ask = "新居先生の目を見本どおりに戻して"
    prompt, refs, source = _run(tmp_path, monkeypatch, _entry(edit_instruction=ask))
    assert generator.instruction_edit_prompt(ask) in prompt
    assert prompt.startswith(generator._EDIT_CHARACTER_REFERENCE_INSTRUCTION)
    assert isinstance(refs, list) and len(refs) == 5
    assert refs[0][0] == source


@pytest.mark.parametrize("ask, role", [
    ("男性のサラリーマンに", "ordinary"), ("先生を一般の男性に変える", "ordinary"),
    ("新井先生ではなく会社員に", "ordinary"), ("先生の隣に会社員を追加", "ordinary"),
    ("新居先生の顔はそのまま、右の男性の目だけ丸く", "ordinary"),
    ("新居先生にはしないで。普通の男性にして", "ordinary"),
    ("先生の目になっているので、一般の男性に戻して", "ordinary"),
    ("先生の目が強すぎるので会社員の顔に戻す", "ordinary"),
    ("新居先生本人の目を直す", "professor"),
    ("先生を消して", "source"), ("新井先生のまぶたを元に戻す", "professor"),
    ("左の会社員を新居先生に変更", "professor"), ("矢印を消す", "source")])
def test_edit_subject_follows_the_current_request(ask, role):
    assert edit_subject(ask) == role


def test_generic_man_in_a_professor_scene_is_not_given_any_professor_sample(tmp_path, monkeypatch):
    entry = _entry(character=True, prompt="The professor points at the arrow.", edit_instruction="男性のサラリーマンに")
    prompt, refs, source = _run(tmp_path, monkeypatch, entry)
    assert refs == source and generator._ORDINARY_PERSON_EDIT in prompt
    assert "professor" not in prompt and "Variant" not in prompt


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
