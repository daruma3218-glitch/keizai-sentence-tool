import base64
from io import BytesIO
import json
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageOps

import generator
import material_regions as regions
import material_store as store
from test_materials import choose, make_job, studio


POLYGONS = [[[100, 200], [300, 200], [300, 500], [100, 500]],
            [[650, 600], [900, 600], [775, 900]]]


@pytest.mark.parametrize("raw", [None, "", "[]", "{}", "bad", "[[[1,1],[2,2],[3,3]]]",
                                 "[[[false,0],[40,0],[0,50]]]", "[[[-1,0],[40,0],[0,50]]]",
                                 json.dumps(POLYGONS[:1] * 9), json.dumps([[[0, 0]] * 129])])
def test_invalid_selection_is_rejected(raw):
    with pytest.raises(ValueError):
        regions.parse_regions(raw)


@pytest.mark.parametrize("provider", ["gpt-image", "nanobanana"])
def test_two_regions_through_candidate_route_and_provider(studio, monkeypatch, provider):
    client, headers, root = studio
    job, _, _ = make_job(root)
    context = store.read(job / "material_context.json")
    context["profile"].update(provider=provider, type_providers={"diagram": provider},
                              character_ref="references/keizai_professor.png")
    store.save(job / "material_context.json", context)
    source_path = job / "images" / "portrait.png"
    with Image.new("RGBA", (61, 97), (5, 15, 25, 167)) as im:
        im.putpixel((0, 0), (121, 222, 187, 0))
        im.save(source_path)
    library = store.load_library(job)
    candidate = store.snapshot(job, library, 1, source_path, "元画像")
    store.commit_library(job, library, "fixture")
    choose(job, candidate)
    original_bytes = (job / candidate["filename"]).read_bytes()

    monkeypatch.setenv("KEIZAI_OPENAI_API_KEY", "offline-key")
    monkeypatch.setenv("KEIZAI_GEMINI_API_KEY", "offline-key")
    calls = []
    def generated():
        with BytesIO() as output, Image.new("RGB", regions.FRAME, "green" if len(calls) == 1 else "red") as image:
            image.save(output, format="PNG")
            return output.getvalue()

    def verify_inputs(prompt, source, mask):
        assert generator._ORDINARY_PERSON_EDIT in prompt and "professor" not in prompt
        with Image.open(BytesIO(source)) as ref, Image.open(BytesIO(mask)) as selection:
            assert ref.size == selection.size == regions.FRAME
            # Portrait content is centered in the common landscape reference.
            assert ref.getpixel((0, 0)) == (255, 255, 255, 255)
            assert max(abs(a - b) for a, b in zip(ref.getpixel((768, 432)), (5, 15, 25, 167))) <= 1
            if provider == "gpt-image":
                assert selection.getpixel((0, 0))[-1] == 255 and selection.getpixel((608, 300))[-1] == 0
            else:
                assert selection.getpixel((0, 0)) == 0 and selection.getpixel((608, 300)) == 255
        calls.append(prompt)

    def openai(images, method, consume, **kwargs):
        assert provider == "gpt-image" and method == "edit" and kwargs["size"] == "1536x864"
        verify_inputs(kwargs["prompt"], kwargs["image"].getvalue(), kwargs["mask"].getvalue())
        return consume(SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(generated()), url=None)]))

    def gemini(client, consume, **kwargs):
        assert provider == "nanobanana" and kwargs["config"].image_config.aspect_ratio == "16:9"
        source, mask, prompt = kwargs["contents"]
        verify_inputs(prompt, source.inline_data.data, mask.inline_data.data)
        return consume(SimpleNamespace(parts=[SimpleNamespace(inline_data=SimpleNamespace(mime_type="image/png", data=generated()))]))

    monkeypatch.setattr(generator, "consume_openai_image", openai)
    monkeypatch.setattr(generator, "consume_gemini_image", gemini)
    body = {"kind": "edit", "no": 1, "candidate_id": candidate["id"], "request_id": "bounded-edit",
            "revision": store.load_library(job)["revision"], "count": 2, "instruction": "男性のサラリーマンに",
            "edit_scope": "regions", "regions": json.dumps(POLYGONS)}
    response = client.post("/api/material-jobs/sample/variant", data=body, headers=headers)
    assert response.status_code == 200, response.text
    library = response.get_json()["library"]
    assert library["selections"]["1"] == candidate["id"] and len(calls) == 2
    added = [c for c in library["candidates"]["1"] if c.get("parent_id") == candidate["id"]]
    assert len(added) == 2 and all(c["label"] == "部分修正案" for c in added)
    with Image.open(source_path) as src, Image.new("L", src.size, 0) as expected:
        draw = ImageDraw.Draw(expected)
        draw.rectangle((6, 19, 18, 48), fill=255)
        draw.polygon([(39,58),(54,58),(46,86)], fill=255)
        for item in added:
            with Image.open(job / item["filename"]) as output:
                assert output.size == src.size
                assert all((a != b) == (s != 0) for a,b,s in zip(src.getdata(), output.getdata(), expected.getdata()))
    assert (job / candidate["filename"]).read_bytes() == original_bytes
    assert client.post("/api/material-jobs/sample/variant", data=body, headers=headers).status_code == 200
    assert len(calls) == 2


def test_orientation_matches_browser_for_jpeg(tmp_path):
    source_path, result_path = tmp_path / "sideways.jpg", tmp_path / "result.png"
    with Image.new("RGB", (24, 40), "white") as im:
        exif = im.getexif()
        exif[274] = 6
        im.save(source_path, exif=exif)
    region = regions.prepare(source_path, POLYGONS, "nanobanana")
    with Image.new("RGB", regions.FRAME, "green") as changed:
        changed.save(result_path)
    regions.restore_outside(source_path, result_path, region, POLYGONS)
    with Image.open(source_path) as src, Image.open(result_path) as result:
        assert result.size == ImageOps.exif_transpose(src).size == (40, 24)
        assert result.getpixel((8, 8)) == (0, 128, 0)
        assert result.getpixel((20, 8)) == (255, 255, 255)


@pytest.mark.parametrize("channel, fields", [
    ("economy", {"edit_scope":"regions"}), ("economy", {"edit_scope":"regions", "regions":"[]"}),
    ("economy", {"regions":json.dumps(POLYGONS)}),
    ("russia", {"edit_scope":"regions", "regions":json.dumps(POLYGONS)})])
def test_invalid_edit_does_not_enter_generation(studio, channel, fields):
    client, headers, root = studio
    job, _, candidate = make_job(root, channel=channel)
    body = {"kind":"edit", "no":1, "candidate_id":candidate["id"], "request_id":"bad-selection",
            "revision":1, "instruction":"消す", **fields}
    assert client.post("/api/material-jobs/sample/variant", data=body, headers=headers).status_code == 409
    assert not store.load_library(job)["requests"]
