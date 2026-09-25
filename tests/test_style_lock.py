#!/usr/bin/env python3
"""世界観ロック（チャンネル設定 style_lock / allow_ai_realphoto）の pytest。

2026-09-25 カラクリ経済学: ルノアール回で新居先生が約250枚中37枚を手で差し替えた
（厚塗り・劇画調・アニメ調・青パーカーの若者→フラットな先生、AI実写風→Commonsの写真）。
原因は「内容に合う画風を水彩・3D・コミック等から選べ」という生成指示、プリセット配色
（カラーコード付き）と世界観の食い違い、世界観の設定文にあった「若い学生も登場してよい」。

受け入れ基準:
- ロック中は世界観の設定文が illustration / diagram / chart / decorative の生成指示の先頭に必ず入り、
  画風を選ばせる指示とプリセット配色（カラーコード）が入らない
- ロックなし（他チャンネル）の生成指示は従来のまま
- AI実写風なしのチャンネルでは realphoto を世界観イラストにし、Web写真の代替もイラストにする
- 画像生成・指示文生成の全呼び出し箇所がロックを渡している（渡し忘れを構造的に防ぐ）
"""
import ast
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

import app as appmod  # noqa: E402
import generator  # noqa: E402
import pipeline as plmod  # noqa: E402
import prompter  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LOCK = "ART STYLE: flat cartoon with bold outlines.\nTHE PROFESSOR: round face, glasses."
HEX = re.compile(r"#[0-9A-Fa-f]{6}\b")


def test_keizai_channel_locks_its_world():
    ch = appmod.get_channel("keizai")
    d = ch["defaults"]
    assert ch["name"] == "日本カラクリ経済学"
    assert d["style_lock"] is True
    assert d["allow_ai_realphoto"] is False
    assert d["worldview_mode"] is True
    assert (ROOT / d["character_ref"]).exists()
    world = d["worldview_desc"]
    # 参照画像（references/keizai_professor.png）に合わせた先生の特徴
    for feature in ("THE PROFESSOR", "half-lidded", "closed-mouth", "no shirt collar", "speckled tweed"):
        assert feature in world
    # 別の固定キャラを作らない・崩れた画風を名指しで禁止
    assert "no young student" in world and "no hoodie character" in world
    for banned in ("photorealistic", "3D", "painterly", "gekiga", "anime", "watercolor"):
        assert banned in world
    # 旧設定の食い違い（白い襟・学生の副キャラ）とカラーコードは入れない
    assert "white collar" not in world
    assert "university student may appear" not in world
    assert not HEX.search(world)


def test_other_channels_do_not_lock():
    for channel_id in ("default", "roshia", "seikou"):
        d = appmod.get_channel(channel_id)["defaults"]
        assert not d.get("style_lock")
        assert d.get("allow_ai_realphoto", True) is True


def test_locked_prompt_puts_world_first_and_drops_style_roulette_and_hex():
    for prompt_type in ("illustration", "diagram", "chart", "decorative"):
        p = generator._build_full_prompt("A cafe table with a coffee cup.", prompt_type,
                                         allowed_terms=["コーヒー"], style_preset="flat_infographic",
                                         style_lock=LOCK)
        assert p.startswith("CHANNEL ART STYLE")
        assert LOCK in p
        assert "choose the most fitting illustration style" not in p
        assert "PRESET STYLE" not in p
        assert not HEX.search(p), prompt_type
        assert p.rstrip().endswith("keep the channel style.")
        assert "Content to visualize:\nA cafe table with a coffee cup." in p
    diagram = generator._build_full_prompt("x", "diagram", style_lock=LOCK)
    assert "clean conceptual diagram with arrows" in diagram  # 図解の組み立て指示は残す


def test_unlocked_prompts_are_unchanged():
    p = generator._build_full_prompt("A cafe.", "illustration", style_preset="flat_infographic")
    assert p.startswith("Style: choose the most fitting illustration style")
    assert "PRESET STYLE: flat educational infographic" in p
    assert "#D9E1E8" in p
    assert "CHANNEL ART STYLE" not in p
    # 実写・地図はロック中でも固有の見た目のまま
    for prompt_type in ("realphoto", "map"):
        locked = generator._build_full_prompt("A street.", prompt_type, style_preset="flat_infographic", style_lock=LOCK)
        assert locked == generator._build_full_prompt("A street.", prompt_type, style_preset="flat_infographic")


def test_generator_applies_lock_to_every_image_and_keeps_character_reference(tmp_path, monkeypatch):
    gen = generator.ParallelImageGenerator(
        provider=generator.PROVIDER_GPT_IMAGE, openai_api_key="test-only",
        style_preset="flat_infographic", concurrency=2,
        reference_image_path=str(ROOT / "references" / "keizai_professor.png"),
        style_lock_text=LOCK,
    )
    seen = []

    def fake_dispatch(full_prompt, output_path, use_reference=False, ref_bytes_override=None, ref_mime_override=None):
        seen.append((Path(output_path).name, full_prompt, use_reference))
        Image.new("RGB", (32, 18), (220, 225, 232)).save(output_path)
        return True, ""

    monkeypatch.setattr(gen, "_dispatch_sync_generate", fake_dispatch)
    prompts = [
        {"index": 1, "prompt": "The professor explains with open hands.", "type": "illustration", "character": True},
        {"index": 2, "prompt": "Cup -> shop -> coins.", "type": "diagram"},
        {"index": 3, "prompt": "A shop owner counts receipts.", "type": "illustration"},
    ]
    results = asyncio.run(gen.generate_all(prompts, tmp_path))
    assert all(r["success"] for r in results)
    by_name = {name: (prompt, ref) for name, prompt, ref in seen}
    prof_prompt, prof_ref = by_name["1.png"]
    assert prof_ref is True
    assert prof_prompt.startswith(generator._CHARACTER_LOCK_INSTRUCTION)
    assert "CHANNEL ART STYLE" in prof_prompt
    for name in ("2.png", "3.png"):
        prompt, ref = by_name[name]
        assert prompt.startswith("CHANNEL ART STYLE") and ref is False


def _pipe(tmp_path, **kw):
    return plmod.SentencePipeline(manuscript_text="x" * 200, output_dir=tmp_path / "job",
                                  channel_id="keizai", **kw)


def test_realphoto_becomes_world_illustration_when_ai_realphoto_is_off(tmp_path):
    pipe = _pipe(tmp_path, worldview_desc=LOCK, style_lock=True, allow_ai_realphoto=False)
    routes = {
        1: {"route": "realphoto", "reason": "喫茶店の店内"},
        2: {"route": "web_photo", "reason": "実在の店舗"},
        3: {"route": "diagram", "reason": "仕組み"},
    }
    assert pipe._apply_realphoto_policy(routes) == 1
    assert routes[1]["route"] == "illustration" and "AI実写風なし" in routes[1]["reason"]
    assert routes[2]["route"] == "web_photo"  # 本物の写真はそのまま
    assert routes[3]["route"] == "diagram"
    assert pipe._realphoto_fallback_route() == "illustration"


def test_realphoto_policy_is_off_by_default(tmp_path):
    pipe = _pipe(tmp_path)
    routes = {1: {"route": "realphoto", "reason": "街"}}
    assert pipe._apply_realphoto_policy(routes) == 0
    assert routes[1]["route"] == "realphoto"
    assert pipe._realphoto_fallback_route() == "realphoto"


def test_style_lock_needs_world_text(tmp_path):
    off = _pipe(tmp_path, worldview_desc="", style_lock=True)  # 世界観モードOFFのジョブ
    assert off.style_lock is False and off.style_lock_text == ""
    on = _pipe(tmp_path, worldview_desc=LOCK, style_lock=True)
    assert on.style_lock is True and on.style_lock_text == LOCK


def test_prompter_lock_replaces_preset_palette_and_sets_people_rules(monkeypatch):
    block = prompter._build_user_block("", "flat_infographic", style_lock=True)
    assert "図解の組み立て" in block and not HEX.search(block)
    assert "#D9E1E8" in prompter._build_user_block("", "flat_infographic")  # ロックなしは従来どおり

    captured = {}

    def fake_query(client, query, system, **kw):
        captured["text"] = "\n".join(part.get("text", "") for part in query) if isinstance(query, list) else str(query)
        return '[{"no": 1, "prompt": "The professor asks the viewer a question.", "type": "illustration", "allowed_terms": [], "diagram_blueprint": {}, "character": true}]'

    monkeypatch.setattr(prompter, "claude_query", fake_query)
    rows = [{"no": 1, "sentence": "あなたならどうしますか？", "route": "illustration"}]
    out = prompter.generate_prompts_batch(object(), rows, "テスト", style_preset="flat_infographic",
                                          worldview_desc=LOCK, style_lock=True)
    text = captured["text"]
    assert "世界観ロック" in text and LOCK in text
    assert "唯一の固定キャラ" in text and "若い学生・パーカー姿" in text
    assert "画風・画材・色・照明を指定する語" in text
    assert not HEX.search(text.split("入力（type=")[1])  # 動的部分にプリセットのカラーコードが無い
    assert out[0]["character"] is True


def _calls(path, func_name):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            if name == func_name:
                yield node


def test_every_generation_call_site_passes_the_lock():
    """新しい生成経路が増えてもロックの渡し忘れを見逃さない（v2.8.38 の手写し漏れの教訓）。"""
    for path in (ROOT / "app.py", ROOT / "pipeline.py"):
        gen_calls = list(_calls(path, "run_parallel_generation"))
        prompt_calls = list(_calls(path, "generate_all_prompts"))
        assert gen_calls, path
        for call in gen_calls:
            assert "style_lock_text" in {k.arg for k in call.keywords}, f"{path.name}:{call.lineno}"
        for call in prompt_calls:
            assert "style_lock" in {k.arg for k in call.keywords}, f"{path.name}:{call.lineno}"


def test_style_lock_text_for_jobs():
    locked = {"style_lock": True, "worldview_desc": "CHANNEL NOW"}
    assert appmod._style_lock_text_for({"worldview_desc": "x"}) == ""  # ロックなしチャンネル
    assert appmod._style_lock_text_for(locked) == "CHANNEL NOW"
    # 世界観モードOFFで作ったジョブ → ロックしない
    assert appmod._style_lock_text_for(locked, {"worldview_desc": ""}, None) == ""
    # ロック付きで作ったジョブ → そのジョブの世界観で描き直す
    assert appmod._style_lock_text_for(locked, {"worldview_desc": "JOB", "style_lock": True}) == "JOB"
    assert appmod._style_lock_text_for(locked, {}, {"worldview_desc": "JOB", "style_lock": True}) == "JOB"
    # ロック導入前のジョブ → チャンネルの現在の世界観
    assert appmod._style_lock_text_for(locked, {}, {"worldview_desc": "OLD"}) == "CHANNEL NOW"


def test_scene_fix_prompt_drops_conflicting_tone_when_locked():
    defaults = {"worldview_desc": LOCK}
    unlocked = appmod._build_scene_fix_prompt("先生が説明する。", "illustration", 1, "", defaults)
    assert "should not become cute" in unlocked and "Visual world / tone" in unlocked
    locked = appmod._build_scene_fix_prompt("先生が説明する。", "illustration", 1, "", defaults, style_locked=True)
    assert "should not become cute" not in locked and "Visual world / tone" not in locked
