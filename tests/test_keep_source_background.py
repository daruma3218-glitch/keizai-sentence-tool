"""コメントの手直しで、前の手直しで変えた背景が場面の色へ戻らないこと（2026-11-15 社長「直してほしい」）。

本番の試験の回 20261115_010251_1b39f2 で、1回目の手直し「背景を淡い水色に」の後、2回目の手直し
「先生を少し左へ」で背景が場面の指定色（桃色）に戻った。手直しは毎回「場面の最初の指示＋今回の指示」
で作るうえ、世界観の設定文の「BACKDROP COLORS」から場面の色を指定し直していたため。

- 元画像を直す時は、色・板を選ばず「元画像の背景を保つ（指示があれば変える）」を渡す
- 新しく作る時は、今までどおり段落ごとの色・画像ごとの板
"""
import asyncio
import json
from pathlib import Path

from PIL import Image

import generator

ROOT = Path(__file__).resolve().parents[1]
PRIMARY = ROOT / "references" / "keizai_professor.png"
WORLD = next(c for c in json.loads((ROOT / "channels.json").read_text(encoding="utf-8"))["channels"]
             if c["id"] == "keizai")["defaults"]["worldview_desc"]


def test_new_image_still_gets_the_paragraph_color_and_board():
    scene = generator._build_full_prompt("x", "illustration", style_lock=WORLD, backdrop=4)
    assert "BACKDROP COLOR OF THIS IMAGE: pale dusty rose" in scene
    assert generator._KEEP_SOURCE_BACKGROUND not in scene
    board = generator._build_full_prompt("x", "diagram", style_lock=WORLD, backdrop=0, board=2)
    assert "BOARD OF THIS IMAGE: a green classroom chalkboard surface" in board


def test_edit_keeps_the_source_background_instead_of_the_paragraph_color():
    scene = generator._build_full_prompt("x", "illustration", style_lock=WORLD, backdrop=4,
                                         keep_source_background=True)
    assert generator._KEEP_SOURCE_BACKGROUND in scene
    assert "BACKDROP COLOR OF THIS IMAGE" not in scene and "BACKDROP COLORS:" not in scene
    assert "pale dusty rose" not in scene
    # 場所を描く・無地にする使い分けの決まりは残す（新しく描く部分のため）
    assert "BACKGROUNDS OF STORY SCENES" in scene

    board = generator._build_full_prompt("x", "diagram", style_lock=WORLD, backdrop=0, board=2,
                                         keep_source_background=True)
    assert "BOARD OF THIS IMAGE: the same board surface as the attached source image." in board
    assert "chalkboard surface, the shapes" not in board and "DIAGRAM BOARDS:" not in board


def test_lock_without_backdrop_list_is_unchanged_when_editing():
    assert generator.apply_backdrop("ART STYLE: flat.", "illustration", 3, keep_source=True) == "ART STYLE: flat."


def _run(tmp_path, monkeypatch, entry, edit=True):
    source = tmp_path / "source.png"
    Image.new("RGB", (32, 18), (177, 214, 238)).save(source)
    gen = generator.ParallelImageGenerator(
        provider=generator.PROVIDER_GPT_IMAGE, openai_api_key="test-only",
        style_preset="flat_infographic", concurrency=1, reference_image_path=str(PRIMARY),
        edit_image_path=str(source) if edit else None, style_lock_text=WORLD)
    seen = []

    def fake_dispatch(full_prompt, output_path, use_reference=False, ref_bytes_override=None, ref_mime_override=None):
        seen.append(full_prompt)
        Image.new("RGB", (32, 18), (220, 225, 232)).save(output_path)
        return True, ""

    monkeypatch.setattr(gen, "_dispatch_sync_generate", fake_dispatch)
    assert asyncio.run(gen.generate_all([entry], tmp_path / "out"))[0]["success"]
    return seen[0]


def test_comment_fix_on_professor_scene_keeps_background(tmp_path, monkeypatch):
    (tmp_path / "out").mkdir()
    entry = {"index": 6, "prompt": "The professor holds a package toward the viewer.\n\n"
                                   "Additional instruction: 先生の立ち位置を少し左へ。",
             "type": "illustration", "character": True, "edit_source": True,
             "chapter_index": 0, "block_index": 4}
    prompt = _run(tmp_path, monkeypatch, entry)
    assert prompt.startswith(generator._EDIT_SOURCE_INSTRUCTION)
    assert generator._KEEP_SOURCE_BACKGROUND in prompt
    assert "BACKDROP COLOR OF THIS IMAGE" not in prompt


def test_regenerate_without_source_image_still_uses_the_paragraph_color(tmp_path, monkeypatch):
    (tmp_path / "out").mkdir()
    entry = {"index": 6, "prompt": "The professor holds a package toward the viewer.",
             "type": "illustration", "character": True, "chapter_index": 0, "block_index": 4}
    prompt = _run(tmp_path, monkeypatch, entry, edit=False)
    assert "BACKDROP COLOR OF THIS IMAGE: pale dusty rose" in prompt
    assert generator._KEEP_SOURCE_BACKGROUND not in prompt
