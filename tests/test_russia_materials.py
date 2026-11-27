import base64
import hashlib
import json
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from PIL import Image

import generator
import material_review
import material_store as store
import russia_map_renderer
import russia_materials
import subscription_runtime
from test_materials import choose, make_job, studio


def post(client, headers, job, source, request_id, kind, **fields):
    body = dict(no=1, kind=kind, candidate_id=source["id"], request_id=request_id,
                revision=store.load_library(job)["revision"])
    body.update(fields)
    return client.post(f"/api/material-jobs/{job.name}/variant", headers=headers, data=body)


@pytest.mark.parametrize("provider,channel", [("gpt-image", "russia"), ("nanobanana", "roshia")])
def test_provider_enforces_background_for_generation_and_edits(tmp_path, monkeypatch, provider, channel):
    import openai

    prompts = []
    def respond(**args):
        if "prompt" in args:
            prompts.append(args["prompt"])
            return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(png))])
        contents = args["contents"]
        prompts.append(contents[-1] if isinstance(contents, list) else contents)
        return SimpleNamespace(parts=[SimpleNamespace(inline_data=SimpleNamespace(data=png, mime_type="image/png"))])

    monkeypatch.setattr(openai, "OpenAI", lambda **_: SimpleNamespace(images=SimpleNamespace(generate=respond, edit=respond)))
    monkeypatch.setattr(generator.genai, "Client", lambda **_: SimpleNamespace(models=SimpleNamespace(generate_content=respond)))
    monkeypatch.setattr(generator, "MAX_RETRIES", 3)
    gen = generator.ParallelImageGenerator(provider, openai_api_key="offline", gemini_api_key="offline", channel_id=channel)
    path = tmp_path / "result.png"
    data = BytesIO()
    with Image.new("RGBA", (96, 64), (0, 0, 0, 0)) as image:
        image.paste((12, 15, 19, 255), (20, 20, 75, 45))  # A dark foreground subject is valid.
        image.save(data, "PNG")
    png = data.getvalue()
    assert gen._dispatch_sync_generate("scene", path)[0]
    original = path.read_bytes()
    with Image.open(path) as saved:
        assert saved.mode == "RGB"
        assert saved.getpixel((0, 0)) == saved.getpixel((14, 2)) == (217, 225, 232)
        assert saved.getpixel((50, 32)) == (12, 15, 19)

    data = BytesIO()
    with Image.new("RGB", (96, 54), (11, 12, 15)) as image:
        image.paste("#D9E1E8", (20, 14, 75, 40))
        image.save(data, "PNG")
    png = data.getvalue()
    assert gen._dispatch_sync_generate("edit", path, ref_bytes_override=original) == (
        False, russia_materials.BLACK_BACKGROUND_MESSAGE)
    assert len(prompts) == 2 and all(russia_materials.BACKGROUND_RULE in p for p in prompts)
    assert path.read_bytes() == original
    generator._save_as_16_9(png, path)  # Other channels can use the same dark art.
    with Image.open(path) as saved:
        assert saved.getpixel((0, 0)) == (11, 12, 15)


def test_indexed_transparency_and_black_corners(tmp_path):
    output = tmp_path / "result.png"
    for mode in ("P", "RGB"):
        data = BytesIO()
        with Image.new(mode, (96, 64), 0) as image:
            if mode == "P":
                image.putpalette([0, 0, 0, 27, 54, 93] + [0] * 762)
                image.paste(1, (36, 24, 60, 40))
                image.save(data, "PNG", transparency=0)
            else:
                image.paste("#D9E1E8", (0, 0, 96, 64))
                for x in (0, 46, 92):
                    for y in (0, 30, 60):
                        image.paste("black", (x, y, x+4, y+4))
                image.save(data, "PNG")
        generator._save_as_16_9(data.getvalue(), output, light_background=True)
        with Image.open(output) as saved:
            assert saved.getpixel((0, 0)) == (217, 225, 232)
            assert saved.getpixel((57, 32)) == ((27, 54, 93) if mode == "P" else (0, 0, 0))


def test_black_variant_reports_the_reason_and_keeps_the_adoption(studio, monkeypatch):
    client, headers, root = studio
    job, _, original = make_job(root, channel="russia")
    choose(job, original)
    before = store.load_library(job)
    monkeypatch.setenv("ROSHIA_OPENAI_API_KEY", "offline-test")
    monkeypatch.setenv("ROSHIA_GEMINI_API_KEY", "offline-test")
    monkeypatch.setattr(generator, "run_parallel_generation", lambda **_: [
        {"success":False, "filename":None, "error":russia_materials.BLACK_BACKGROUND_MESSAGE}])
    response = post(client, headers, job, original, "dark-background", "generate", target_type="diagram")
    assert response.status_code == 409 and response.json["error"] == russia_materials.BLACK_BACKGROUND_MESSAGE
    after = store.load_library(job)
    assert after["requests"]["dark-background"]["status"] == "failed"
    assert before["candidates"] == after["candidates"] and before["selections"] == after["selections"]


def test_edits_retypes_and_exports_keep_the_source(studio, monkeypatch):
    client, headers, root = studio
    job, _, original = make_job(root, channel="russia")
    choose(job, original)
    ctx = store.read(job / "material_context.json")
    ctx["profile"].update(type_providers={"diagram": "gpt-image", "realphoto": "nanobanana"})
    store.save(job / "material_context.json", ctx)
    for key in ("ROSHIA_OPENAI_API_KEY", "ROSHIA_GEMINI_API_KEY"):
        monkeypatch.setenv(key, "offline-test")
    requests = []

    def generated(**opts):
        requests.append(opts)
        with Image.new("RGB", (32, 18), (14 * len(requests), 34, 56)) as result:
            result.save(opts["output_dir"] / "one.png")
        return [{"success": True, "filename": "one.png"}]

    monkeypatch.setattr(generator, "run_parallel_generation", generated)
    endpoint = client.get(f"/materials/jobs/{job.name}")
    assert 'id="review-image-actions"' in endpoint.text
    for target in ("realphoto", "illustration", "diagram"):
        result = post(client, headers, job, original, "as-" + target, "generate", target_type=target)
        assert result.status_code == 200, result.text
        library = result.json["library"]
        new = library["candidates"]["1"][-1]
        opts = requests[-1]
        assert opts["channel_id"] == "russia"
        assert new["parent_id"] == original["id"] and new["route"] == target
        assert opts["edit_image_path"] is None and opts["prompts"][0]["type"] == target
        assert opts["realphoto_watermark"] == (target == "realphoto")
        assert library["selections"]["1"] == original["id"]
        if target == "realphoto":
            photo = new
        if target == "diagram":
            assert "Variant B" in opts["prompts"][0]["prompt"]

    for preset in ("remove_text", "add_icons"):
        result = post(client, headers, job, photo, preset, "edit", preset=preset)
        assert result.status_code == 200, result.text
        edited = result.json["library"]["candidates"]["1"][-1]
        opts = requests[-1]
        assert edited["parent_id"] == photo["id"] and edited["route"] == "realphoto"
        assert opts["provider"] == "nanobanana" and opts["edit_image_path"] == str(job / photo["filename"])
        assert opts["prompts"][0]["edit_mode"] == "instruction_only"
        assert ("文字" if preset == "remove_text" else "アイコン") in opts["prompts"][0]["edit_instruction"]
        assert post(client, headers, job, photo, preset, "edit", preset=preset).status_code == 200
    assert len(requests) == 5
    assert store.load_library(job)["selections"]["1"] == original["id"]
    assert store.digest(job / original["filename"]) == original["sha256"]
    choose(job, photo)
    exported = material_review.review_rows(job)[1]
    assert exported[0]["values"][9] == exported[1]["values"][9] == "実写風"
    with ZipFile(BytesIO(client.get(f"/materials/jobs/{job.name}/download").data)) as bundle:
        assert json.loads(bundle.read("material-handoff.json"))["rows"][0]["asset"]["id"] == photo["id"]


def test_manual_map_renders_studio_borders_without_image_api(studio, monkeypatch):
    client, headers, root = studio
    job, _, original = make_job(root, channel="russia")
    rows = store.read(job / "rows_progress.json")
    rows["rows"][0].update(sentence="日本からロシアへの航路を見ます。")
    store.save(job / "rows_progress.json", rows)
    choose(job, original)
    monkeypatch.setattr(generator, "run_parallel_generation", lambda **_: pytest.fail("画像APIを呼び出しました"))

    def design(system, query, **settings):
        assert settings["channel"] == "roshia" and settings["tool"] == "sentence"
        assert not settings["notify"] and "航路" in json.loads(query)["sentence"]
        return '{"focus":["JPN","RUS"],"arrows":[{"from":"JPN","to":"RUS"}]}', {}

    monkeypatch.setattr(subscription_runtime, "generate", design)
    got = post(client, headers, job, original, "map-1", "generate", target_type="map")
    assert got.status_code == 200, got.text
    lib = got.json["library"]
    asset = lib["candidates"]["1"][-1]
    assert asset["route"] == "map" and asset["parent_id"] == original["id"]
    assert lib["selections"]["1"] == original["id"] and lib["requests"]["map-1"]["requested_images"] == 0
    assert hashlib.sha256(russia_map_renderer.DATA_PATH.read_bytes()).hexdigest() == "a01804909d67a94ea2b49304715e7658ab28c487facdc725d91605b76497281d"
    with Image.open(job / asset["filename"]) as image:
        assert image.size == (1920, 1080)
        image.verify()
    monkeypatch.setattr(subscription_runtime, "generate", lambda *a, **kw: ('{"drawable": false}', {}))
    result = post(client, headers, job, original, "unplaced", "generate", target_type="map")
    assert result.status_code == 409 and "場所を特定" in result.json["error"]
    lib = store.load_library(job)
    assert lib["requests"]["unplaced"]["status"] == "failed" and len(lib["candidates"]["1"]) == 2


@pytest.mark.parametrize("channel,kind,fields", [
    ("economy", "edit", {"preset": "add_icons"}),
    ("economy", "generate", {"target_type": "map"}),
    ("russia", "edit", {"preset": "bad"}),
    ("russia", "generate", {"target_type": "bad"}),
    ("russia", "edit", {"preset": "remove_text", "count": "2"}),
    ("russia", "generate", {"target_type": "map", "candidate_id": "another-scene"}),
])
def test_invalid_controls_do_not_start_generation(studio, channel, kind, fields):
    client, headers, root = studio
    job, _, original = make_job(root, channel=channel)
    if channel != "russia":
        assert 'id="review-image-actions"' not in client.get(f"/materials/jobs/{job.name}").text
    assert post(client, headers, job, original, "invalid", kind, **fields).status_code == 409
    assert not store.load_library(job)["requests"]
