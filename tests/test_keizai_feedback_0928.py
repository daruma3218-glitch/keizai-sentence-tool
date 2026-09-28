"""新居先生の感想（2026-09-28）の反映。

①ほかの人物の目が先生と同じ半目で違和感 → 先生以外は普通に開いた目
②イラストと図のタッチが同じ → 図解は白に近い紙の背景の説明ボード
③背景が全部似たような色 → 段落ごとに背景の色を変える
④グラフは「データのみ」と「先生がスクリーンで紹介」の2つの版を持ち、入れ替えられる
"""
import json
from types import SimpleNamespace

import app as appmod
import generator
from chart_restyle import restyle_chart_file, use_chart_variant, variant_path
from test_chart_restyle import _FakeImages, _FakeVerify, _job

WORLD = appmod.get_channel("keizai")["defaults"]["worldview_desc"]


# ── ① ほかの人物の目
def test_other_people_have_open_eyes_in_world_and_every_lock():
    assert "Other people have ordinary open eyes" in WORLD
    for lock in (generator._CHARACTER_LOCK_INSTRUCTION, generator._CHARACTER_LOCK_IN_DIAGRAM,
                 generator._STYLE_REFERENCE_INSTRUCTION):
        assert "belong ONLY to the professor" in lock


# ── ② 図解は説明ボード
def test_locked_diagram_is_an_explanation_board_not_a_scene():
    assert "IMAGE KINDS" in WORLD and "BOARD OF THIS IMAGE" in WORLD
    diagram = generator._build_full_prompt("Cup -> shop -> coins.", "diagram", style_lock=WORLD)
    assert "This image is a DIAGRAM" in diagram
    assert "clean conceptual diagram with arrows" in diagram  # 組み立ての指示は残す
    scene = generator._build_full_prompt("A cafe.", "illustration", style_lock=WORLD)
    assert "This image is a DIAGRAM" not in scene


# ── ③ 背景の色
def test_backdrop_list_is_replaced_by_one_color_per_paragraph():
    first = generator._build_full_prompt("A cafe.", "illustration", style_lock=WORLD,
                                         backdrop=generator.backdrop_key({"chapter_index": 0, "block_index": 0}))
    second = generator._build_full_prompt("A cafe.", "illustration", style_lock=WORLD,
                                          backdrop=generator.backdrop_key({"chapter_index": 0, "block_index": 1}))
    assert "BACKDROP COLORS:" not in first  # 色の一覧は渡さず、この画像の1色だけを渡す
    assert "BACKDROP COLOR OF THIS IMAGE: pale slate blue-gray" in first
    assert "BACKDROP COLOR OF THIS IMAGE: warm light beige" in second
    diagram = generator._build_full_prompt("x", "diagram", style_lock=WORLD, backdrop=1)
    assert "tinted very lightly with warm light beige" in diagram  # 9/28 午後から板の面が画面いっぱい


def test_same_paragraph_same_color_and_regeneration_keeps_it():
    a = generator.backdrop_key({"index": 7, "chapter_index": 2, "block_index": 3})
    b = generator.backdrop_key({"index": 9, "chapter_index": 2, "block_index": 3})
    assert a == b
    assert generator.backdrop_key({"index": 7}) == 7  # 段落が分からない行は画像の番号


def test_lock_without_backdrop_list_is_unchanged():
    assert generator.apply_backdrop("ART STYLE: flat.", "illustration", 3) == "ART STYLE: flat."


def test_generator_passes_paragraph_colors(tmp_path, monkeypatch):
    import asyncio
    from PIL import Image
    gen = generator.ParallelImageGenerator(provider=generator.PROVIDER_GPT_IMAGE, openai_api_key="test-only",
                                           concurrency=1, style_lock_text=WORLD)
    seen = {}

    def fake_dispatch(full_prompt, output_path, use_reference=False, ref_bytes_override=None,
                      ref_mime_override=None):
        from pathlib import Path
        seen[Path(output_path).name] = full_prompt
        Image.new("RGB", (32, 18)).save(output_path)
        return True, ""

    monkeypatch.setattr(gen, "_dispatch_sync_generate", fake_dispatch)
    asyncio.run(gen.generate_all([
        {"index": 1, "prompt": "A shop.", "type": "illustration", "chapter_index": 0, "block_index": 0},
        {"index": 2, "prompt": "A shop.", "type": "illustration", "chapter_index": 0, "block_index": 2},
    ], tmp_path))
    assert "pale slate blue-gray" in seen["1.png"].split("BACKDROP COLOR OF THIS IMAGE:")[1][:40]
    assert "pale sage green" in seen["2.png"].split("BACKDROP COLOR OF THIS IMAGE:")[1][:40]


def test_style_check_sees_the_whole_world_text():
    import verifier
    assert len(WORLD) > 3000
    assert "背景の色は段落ごとに変える決まり" in verifier.STYLE_CHECK_RULES_JA


# ── ④ グラフの2つの版
def _restyle(tmp_path, keep_plain):
    images, ref = _job(tmp_path)
    original = (images / "5.png").read_bytes()
    calls = []
    res = restyle_chart_file(
        images, 5, openai_client=SimpleNamespace(images=_FakeImages(calls)),
        verify_client=_FakeVerify('["100"]', '["100"]'), model="gpt-image-2.5-flare", quality="medium",
        style_lock_text=WORLD, reference_path=str(ref), sentence="昔は、100グラム。", keep_plain=keep_plain,
        backdrop=1)
    return images, original, res, calls


def test_research_chart_keeps_plain_and_gets_a_screen_version(tmp_path):
    images, original, res, calls = _restyle(tmp_path, keep_plain=True)
    assert res == {"restyled": True, "reason": "", "variants": ["plain", "screen"], "variant": "plain"}
    assert (images / "5.png").read_bytes() == original  # 実データのグラフは「データのみ」を使う
    assert variant_path(tmp_path, 5, "screen").exists()
    assert "LAYOUT OF THIS IMAGE: one large green classroom blackboard" in calls[0]["prompt"]  # №5 → 見せ方2
    assert "BACKDROP COLOR OF THIS IMAGE: warm light beige" in calls[0]["prompt"]


def test_other_charts_use_the_screen_version_and_can_switch_back(tmp_path):
    images, original, res, _ = _restyle(tmp_path, keep_plain=False)
    assert res["variant"] == "screen"
    assert (images / "5.png").read_bytes() != original
    assert use_chart_variant(tmp_path, 5, "plain")
    assert (images / "5.png").read_bytes() == original
    assert use_chart_variant(tmp_path, 5, "screen")
    assert (images / "5.png").read_bytes() == variant_path(tmp_path, 5, "screen").read_bytes()
    assert not use_chart_variant(tmp_path, 5, "other")


def test_failed_redraw_drops_the_old_screen_version(tmp_path):
    images, _, _, _ = _restyle(tmp_path, keep_plain=True)
    res = restyle_chart_file(
        images, 5, openai_client=SimpleNamespace(images=_FakeImages([])),
        verify_client=_FakeVerify('["100"]', '["120"]'), model="m", quality="medium")
    assert res["variants"] == ["plain"]
    assert not variant_path(tmp_path, 5, "screen").exists()  # 数字が変わった後の古い版を残さない


# ── 画面の入れ替えと ZIP
def _job_with_rows(tmp_path, monkeypatch, variant="plain"):
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path)
    job = tmp_path / "20260928_090000"
    job.mkdir()
    _restyle(job, keep_plain=True)
    rows = [{"no": 5, "route": "chart", "engine": "render", "status": "ok", "filename": "5.png",
             "chart_variants": ["plain", "screen"], "chart_variant": variant},
            {"no": 6, "route": "illustration", "engine": "ai", "status": "ok", "filename": "6.png"}]
    (job / "rows_progress.json").write_text(json.dumps({"rows": rows}, ensure_ascii=False), encoding="utf-8")
    return job


def test_switch_api_changes_the_image_and_the_row(tmp_path, monkeypatch):
    job = _job_with_rows(tmp_path, monkeypatch)
    client = appmod.app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
    res = client.post(f"/api/chart_variant/{job.name}/5", json={"variant": "screen"})
    assert res.status_code == 200 and res.get_json()["variant"] == "screen"
    assert (job / "images" / "5.png").read_bytes() == variant_path(job, 5, "screen").read_bytes()
    row = json.loads((job / "rows_progress.json").read_text(encoding="utf-8"))["rows"][0]
    assert row["chart_variant"] == "screen"
    assert client.post(f"/api/chart_variant/{job.name}/6", json={"variant": "screen"}).status_code == 400


def test_zip_carries_the_unused_version(tmp_path, monkeypatch):
    job = _job_with_rows(tmp_path, monkeypatch, variant="plain")
    rows = json.loads((job / "rows_progress.json").read_text(encoding="utf-8"))["rows"]
    files = appmod._chart_alternate_files(job, rows)
    assert [arc for _, arc in files] == ["グラフの別版/5_先生が紹介.png"]


def test_supervising_economist_is_the_professor():
    # 9/28 試験・9/27 本番の №11「経済学者の監修のもとで制作」で、先生ではない男性が描かれ画風2点になった
    world = appmod.get_channel("keizai")["defaults"]["worldview_desc"]
    assert "the economist who supervises this channel" in world


def test_backgrounds_are_chosen_by_content():
    # 9/28 社長「内容によってはイラストで描かれたもののほうが動画として使いやすそう。全部じゃなくて使い分け」
    import prompter
    import verifier
    world = appmod.get_channel("keizai")["defaults"]["worldview_desc"]
    assert "BACKGROUNDS OF STORY SCENES" in world and "Illustrated place" in world and "Plain backdrop" in world
    assert "背景の使い分け" in prompter._style_lock_block(world)
    assert "背景の描き込みは減点しない" in verifier.STYLE_CHECK_RULES_JA
    line = generator.apply_backdrop(world, "illustration", 0)
    assert "for an illustrated place, use it as the main tint of its walls or sky" in line
    assert len(world) < 6000  # 画風チェックは設定文を6000字まで渡す


# ── 9/28 午後「まだ単調な雰囲気」: 見せ方・板・構図を変える
def test_number_cards_rotate_their_layout_by_image_number():
    from chart_restyle import CHART_LAYOUTS
    assert len(CHART_LAYOUTS) == 4
    assert len({CHART_LAYOUTS[n % 4] for n in (9, 10, 11, 12)}) == 4  # 続く数字カードは違う見せ方


def test_diagrams_rotate_their_board_and_other_images_drop_the_list():
    first = generator._build_full_prompt("x", "diagram", style_lock=WORLD, board=0)
    third = generator._build_full_prompt("x", "diagram", style_lock=WORLD, board=2)
    assert "BOARD OF THIS IMAGE: a plain, very light paper sheet." in first
    assert "BOARD OF THIS IMAGE: a green classroom chalkboard" in third
    for prompt in (first, third):
        assert "DIAGRAM BOARDS:" not in prompt
    scene = generator._build_full_prompt("A cafe.", "illustration", style_lock=WORLD, board=2)
    assert "DIAGRAM BOARDS:" not in scene and "BOARD OF THIS IMAGE:" not in scene


def test_prompter_varies_composition_and_spaces_out_the_professor():
    import prompter
    block = prompter._style_lock_block(WORLD)
    assert "構図を1枚ごとに変える" in block and "続けて描かない" in block and "3分の1" in block
    assert "図解に変えない" in block and "場面・人・店の様子を語る文は illustration" in block


# ── 9/28 午後「両サイドのベタ塗り・枠。画面いっぱいに」
def test_full_bleed_crops_instead_of_adding_side_bars(tmp_path):
    import io
    from PIL import Image
    buf = io.BytesIO()
    img = Image.new("RGB", (1536, 1024), (200, 60, 60))
    img.paste((20, 20, 200), (0, 0, 1536, 80))          # 上の帯（切られる）
    img.save(buf, format="PNG")
    out = tmp_path / "a.png"
    generator._save_as_16_9(buf.getvalue(), out, fill="crop")
    with Image.open(out) as im:
        assert im.size == (1536, 864)                    # 左右に帯を足さず、上下を切って 16:9
        assert im.getpixel((5, 5)) == (200, 60, 60)      # 端まで絵（帯の色は残らない）
        assert im.getpixel((1530, 430)) == (200, 60, 60)
    generator._save_as_16_9(buf.getvalue(), out)           # 既定は従来どおり（ほかのチャンネル）
    with Image.open(out) as im:
        assert im.size == (1820, 1024)


def test_full_bleed_prompt_and_boards_without_outer_frame():
    assert generator.full_bleed(WORLD)
    scene = generator._build_full_prompt("A cafe.", "illustration", style_lock=WORLD)
    assert "Fill the WHOLE frame edge to edge" in scene and "generous safe margin" not in scene
    diagram = generator._build_full_prompt("x", "diagram", style_lock=WORLD, board=4)
    assert "board surface itself fills the whole frame edge to edge" in diagram
    assert "BOARD OF THIS IMAGE: a light tablet screen surface." in diagram
    assert "thin dark frame" not in WORLD and "thin gray frame" not in WORLD
    plain = generator._build_full_prompt("A cafe.", "illustration", style_lock="ART STYLE: flat.")
    assert "generous safe margin" in plain                 # FULL BLEED の無いチャンネルは従来どおり


def test_generator_asks_for_crop_on_full_bleed_channels(tmp_path, monkeypatch):
    import asyncio
    seen = {}

    def fake_openai(client, full_prompt, output_path, **kw):
        seen["fill"] = kw.get("fill")
        from PIL import Image
        Image.new("RGB", (32, 18)).save(output_path)
        return True, ""

    monkeypatch.setattr(generator, "_sync_generate_image_openai", fake_openai)
    gen = generator.ParallelImageGenerator(provider=generator.PROVIDER_GPT_IMAGE, openai_api_key="test-only",
                                           concurrency=1, style_lock_text=WORLD)
    asyncio.run(gen.generate_all([{"index": 1, "prompt": "A shop.", "type": "illustration"}], tmp_path))
    assert seen["fill"] == "crop"


def test_chart_redraw_is_cropped_and_keeps_numbers_clear_of_the_trim(tmp_path):
    from PIL import Image
    images, original, res, calls = _restyle(tmp_path, keep_plain=False)
    assert "top and bottom 8% are trimmed" in calls[0]["prompt"]
    with Image.open(images / "5.png") as im:
        w, h = im.size
    assert abs(w / h - 16 / 9) < 0.01
