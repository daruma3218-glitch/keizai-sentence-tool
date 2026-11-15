"""先生の見本を複数枚にする（2026-11-15 社長「ドライブに上がっているイラストを参考として取り入れて、
今後はずれイラストがでないようにすること」）。

- references/keizai_professor_set/catalog.json の画像とSHA-256がそろっていること
- 先生が描かれる画像には基準画像＋見本、先生が出ない画像には基準画像だけを渡すこと
- コメントの手直しでは、元画像を1枚目に、先生の見本を後ろに渡すこと（元画像だけだと顔がずれていった）
- OpenAI / Gemini に複数枚の並びで渡ること
"""
import asyncio
import hashlib
import json
from io import BytesIO
from pathlib import Path

from PIL import Image

import generator
from character_refs import load_reference_set, set_dir_for

ROOT = Path(__file__).resolve().parents[1]
PRIMARY = ROOT / "references" / "keizai_professor.png"
LOCK = json.loads((ROOT / "channels.json").read_text(encoding="utf-8"))
LOCK = next(c for c in LOCK["channels"] if c["id"] == "keizai")["defaults"]["worldview_desc"]


def _png(color=(200, 210, 220)):
    bio = BytesIO()
    Image.new("RGB", (32, 18), color).save(bio, format="PNG")
    return bio.getvalue()


def test_keizai_reference_set_matches_catalog():
    folder = set_dir_for(PRIMARY)
    catalog = json.loads((folder / "catalog.json").read_text(encoding="utf-8"))
    assert catalog["primary_sha256"] == hashlib.sha256(PRIMARY.read_bytes()).hexdigest()
    assert len(catalog["images"]) == 3
    for entry in catalog["images"]:
        data = (folder / entry["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"], entry["file"]
    loaded = load_reference_set(PRIMARY)
    assert len(loaded) == 3 and all(m == "image/png" for _, m in loaded)


def test_reference_set_skips_changed_files(tmp_path):
    primary = tmp_path / "prof.png"
    primary.write_bytes(_png())
    folder = tmp_path / "prof_set"
    folder.mkdir()
    good, bad = _png((1, 2, 3)) * 20, _png((9, 9, 9)) * 20
    (folder / "good.png").write_bytes(good)
    (folder / "bad.png").write_bytes(bad)
    (folder / "catalog.json").write_text(json.dumps({"images": [
        {"file": "good.png", "sha256": hashlib.sha256(good).hexdigest()},
        {"file": "bad.png", "sha256": "0" * 64},
        {"file": "missing.png", "sha256": "0" * 64},
        {"file": "../prof.png", "sha256": hashlib.sha256(primary.read_bytes()).hexdigest()},
    ]}), encoding="utf-8")
    assert load_reference_set(primary) == [(good, "image/png")]
    assert load_reference_set(tmp_path / "other.png") == []


def _gen():
    return generator.ParallelImageGenerator(
        provider=generator.PROVIDER_GPT_IMAGE, openai_api_key="test-only",
        style_preset="flat_infographic", concurrency=2,
        reference_image_path=str(PRIMARY), style_lock_text=LOCK)


def test_professor_scenes_get_all_samples_and_others_only_the_primary(tmp_path, monkeypatch):
    gen = _gen()
    assert len(gen.reference_set) == 3
    seen = {}

    def fake_dispatch(full_prompt, output_path, use_reference=False, ref_bytes_override=None, ref_mime_override=None):
        seen[Path(output_path).name] = (full_prompt, use_reference, ref_bytes_override)
        Image.new("RGB", (32, 18), (220, 225, 232)).save(output_path)
        return True, ""

    monkeypatch.setattr(gen, "_dispatch_sync_generate", fake_dispatch)
    prompts = [
        {"index": 1, "prompt": "The professor explains with open hands.", "type": "illustration", "character": True},
        {"index": 2, "prompt": "Cup -> shop -> coins.", "type": "diagram"},
        {"index": 3, "prompt": "The professor stands small beside a flow of coins.", "type": "diagram"},
    ]
    assert all(r["success"] for r in asyncio.run(gen.generate_all(prompts, tmp_path)))

    prompt, use_ref, refs = seen["1.png"]
    assert use_ref is True and len(refs) == 4
    assert refs[0] == (PRIMARY.read_bytes(), "image/png")
    assert prompt.startswith(generator._CHARACTER_LOCK_INSTRUCTION + "\n\n" + generator._REFERENCE_SET_NOTE)
    assert "HIS EYES" in prompt

    prompt, use_ref, refs = seen["3.png"]
    assert use_ref is True and len(refs) == 4
    assert prompt.startswith(generator._CHARACTER_LOCK_IN_DIAGRAM + "\n\n" + generator._REFERENCE_SET_NOTE)

    # 先生が文面に出ない図解は、画風の見本として基準画像1枚だけ（入力の費用を抑える）
    prompt, use_ref, refs = seen["2.png"]
    assert use_ref is True and refs is None
    assert prompt.startswith(generator._STYLE_REFERENCE_INSTRUCTION)


def test_comment_fix_sends_source_first_then_professor_samples(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(_png((10, 20, 30)))
    gen = generator.ParallelImageGenerator(
        provider=generator.PROVIDER_GPT_IMAGE, openai_api_key="test-only",
        style_preset="flat_infographic", concurrency=1,
        reference_image_path=str(PRIMARY), edit_image_path=str(source), style_lock_text=LOCK)
    seen = []

    def fake_dispatch(full_prompt, output_path, use_reference=False, ref_bytes_override=None, ref_mime_override=None):
        seen.append((full_prompt, ref_bytes_override))
        Image.new("RGB", (32, 18), (220, 225, 232)).save(output_path)
        return True, ""

    monkeypatch.setattr(gen, "_dispatch_sync_generate", fake_dispatch)
    prompts = [{"index": 1, "prompt": "Keep only the stealth box; the professor points at it.",
                "type": "illustration", "character": False, "edit_source": True}]
    assert asyncio.run(gen.generate_all(prompts, tmp_path))[0]["success"]
    prompt, refs = seen[0]
    assert refs[0] == (source.read_bytes(), "image/png")
    assert refs[1] == (PRIMARY.read_bytes(), "image/png") and len(refs) == 5
    assert prompt.startswith(generator._EDIT_SOURCE_INSTRUCTION)
    assert generator._EDIT_CHARACTER_REFERENCE_INSTRUCTION in prompt
    # 「添付は先生の見本」という固定の指示（1枚目＝元画像と食い違う）は付けない
    assert generator._CHARACTER_LOCK_INSTRUCTION not in prompt
    assert generator._STYLE_REFERENCE_INSTRUCTION not in prompt


def test_comment_fix_without_style_lock_keeps_source_only(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    source.write_bytes(_png((10, 20, 30)))
    gen = generator.ParallelImageGenerator(
        provider=generator.PROVIDER_GPT_IMAGE, openai_api_key="test-only", concurrency=1,
        reference_image_path=str(PRIMARY), edit_image_path=str(source), style_lock_text="")
    seen = []

    def fake_dispatch(full_prompt, output_path, use_reference=False, ref_bytes_override=None, ref_mime_override=None):
        seen.append(ref_bytes_override)
        Image.new("RGB", (32, 18), (220, 225, 232)).save(output_path)
        return True, ""

    monkeypatch.setattr(gen, "_dispatch_sync_generate", fake_dispatch)
    asyncio.run(gen.generate_all([{"index": 1, "prompt": "Fix labels.", "type": "diagram",
                                   "edit_source": True}], tmp_path))
    assert seen == [source.read_bytes()]


class _Images:
    def __init__(self):
        self.calls = []

    def edit(self, **kw):
        image = kw["image"]
        files = image if isinstance(image, list) else [image]
        self.calls.append([(f.name, f.getvalue()) for f in files])
        bio = BytesIO()
        Image.new("RGB", (1536, 1024), (200, 200, 200)).save(bio, format="PNG")
        import base64
        datum = type("D", (), {"b64_json": base64.b64encode(bio.getvalue()).decode(), "url": None})()
        return type("R", (), {"data": [datum]})()


def test_openai_receives_every_reference_as_a_list(tmp_path):
    images = _Images()
    client = type("C", (), {"images": images})()
    refs = [(_png((1, 1, 1)), "image/png"), (_png((2, 2, 2)), "image/png"), (_png((3, 3, 3)), "image/png")]
    ok, err = generator._sync_generate_image_openai(client, "scene", tmp_path / "out.png",
                                                    reference_bytes=refs, size="1536x1024")
    assert ok, err
    assert [n for n, _ in images.calls[0]] == ["reference1.png", "reference2.png", "reference3.png"]
    assert [d for _, d in images.calls[0]] == [d for d, _ in refs]
    # 1枚のときは従来どおり1つのファイルで渡す
    images.calls.clear()
    ok, _ = generator._sync_generate_image_openai(client, "scene", tmp_path / "out2.png",
                                                  reference_bytes=refs[0][0], size="1536x1024")
    assert ok and [n for n, _ in images.calls[0]] == ["reference.png"]


def test_gemini_receives_every_reference_as_parts(tmp_path, monkeypatch):
    captured = {}

    def fake_consume(client, consume, *, defer_body=False, **kw):
        captured["contents"] = kw["contents"]
        return False, "stop"

    monkeypatch.setattr(generator, "consume_gemini_image", fake_consume)
    monkeypatch.setattr(generator, "MAX_RETRIES", 1)
    refs = [(_png((1, 1, 1)), "image/png"), (_png((2, 2, 2)), "image/jpeg")]
    generator._sync_generate_image_gemini(object(), "scene", tmp_path / "o.png", reference_bytes=refs)
    contents = captured["contents"]
    assert len(contents) == 3 and contents[-1] == "scene"
    assert [p.inline_data.mime_type for p in contents[:2]] == ["image/png", "image/jpeg"]
