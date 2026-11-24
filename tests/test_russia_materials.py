import hashlib
import json
from io import BytesIO
from zipfile import ZipFile

import pytest
from PIL import Image

import generator
import material_review
import material_store as store
import russia_map_renderer
import subscription_runtime
from test_materials import choose, make_job, studio


def post(client, headers, job, source, request_id, kind, **fields):
    body = dict(no=1, kind=kind, candidate_id=source["id"], request_id=request_id,
                revision=store.load_library(job)["revision"])
    body.update(fields)
    return client.post(f"/api/material-jobs/{job.name}/variant", headers=headers, data=body)


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
