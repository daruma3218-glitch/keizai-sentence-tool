import io
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

import app as appmod  # noqa: E402
import generator  # noqa: E402


def _png(path):
    Image.new("RGB", (32, 18), (255, 255, 255)).save(path)


def test_scene_fix_page_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "APP_PASSWORD", "")
    client = appmod.app.test_client()
    resp = client.get("/scene-fix")
    assert resp.status_code == 200
    assert "シーン直しつくーる" in resp.get_data(as_text=True)


def test_scene_fix_api_generates_variants(tmp_path, monkeypatch):
    out_root = tmp_path / "output"
    monkeypatch.setattr(appmod, "OUTPUT_DIR", out_root)
    monkeypatch.setattr(appmod, "APP_PASSWORD", "")
    monkeypatch.setattr(appmod, "get_channel", lambda channel_id: {
        "id": "roshia",
        "defaults": {
            "style_preset": "flat_infographic",
            "type_providers": {"diagram": "gpt-image"},
            "user_instructions": "ロシア解体新書",
        },
    })
    monkeypatch.setattr(appmod, "resolve_channel_keys", lambda channel: {"gemini": "g", "openai": "o", "anthropic": "a"})

    seen = {}
    def fake_generate(prompts, output_dir, **kwargs):
        seen["prompts"] = prompts
        seen["kwargs"] = kwargs
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        out = []
        for p in prompts:
            fname = f"{p['index']}.png"
            _png(Path(output_dir) / fname)
            out.append({"index": p["index"], "success": True, "filename": fname, "provider": kwargs.get("provider")})
        return out

    monkeypatch.setattr(generator, "run_parallel_generation", fake_generate)

    client = appmod.app.test_client()
    resp = client.post("/api/scene-fix", data={
        "channel_id": "roshia",
        "route": "diagram",
        "variant_count": "4",
        "provider": "auto",
        "sentence": "ベラルーシはロシアへの経済依存を深めました。",
        "extra_instruction": "矢印をわかりやすく",
        "fix_mode": "more_clear",
    })

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["tool"] == "scene_fix"
    assert body["provider"] == "gpt-image"
    assert len(body["variants"]) == 4
    assert all(v["ok"] for v in body["variants"])
    assert len(seen["prompts"]) == 4
    assert seen["prompts"][0]["type"] == "diagram"
    assert seen["prompts"][0]["allowed_terms"]
    assert "Prioritize clarity" in seen["prompts"][0]["prompt"]
    manifest = out_root / body["job_id"] / "manifest.json"
    assert manifest.exists()
    saved = json.loads(manifest.read_text(encoding="utf-8"))
    assert saved["sentence"].startswith("ベラルーシ")



def test_scene_fix_reference_image_select_and_zip(tmp_path, monkeypatch):
    out_root = tmp_path / "output"
    monkeypatch.setattr(appmod, "OUTPUT_DIR", out_root)
    monkeypatch.setattr(appmod, "APP_PASSWORD", "")
    monkeypatch.setattr(appmod, "get_channel", lambda channel_id: {
        "id": "roshia",
        "defaults": {
            "style_preset": "flat_infographic",
            "type_providers": {"diagram": "gpt-image"},
        },
    })
    monkeypatch.setattr(appmod, "resolve_channel_keys", lambda channel: {"gemini": "g", "openai": "o", "anthropic": "a"})

    seen = {}
    def fake_generate(prompts, output_dir, **kwargs):
        seen["edit_image_path"] = kwargs.get("edit_image_path")
        seen["prompts"] = prompts
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        out = []
        for p in prompts:
            fname = f"{p['index']}.png"
            _png(Path(output_dir) / fname)
            out.append({"index": p["index"], "success": True, "filename": fname})
        return out

    monkeypatch.setattr(generator, "run_parallel_generation", fake_generate)

    client = appmod.app.test_client()
    resp = client.post("/api/scene-fix", data={
        "channel_id": "roshia",
        "route": "diagram",
        "variant_count": "2",
        "provider": "auto",
        "sentence": "ロシアへの依存が政策判断を縛りました。",
        "fix_mode": "same_style",
        "reference_image": (io.BytesIO(b"fake image bytes"), "source.png"),
    }, content_type="multipart/form-data")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["reference_image"] == "reference/source.png"
    assert seen["edit_image_path"]
    assert Path(seen["edit_image_path"]).exists()
    assert seen["prompts"][0]["edit_source"] is True

    select = client.post(f"/api/scene-fix/{body['job_id']}/select", data={"index": "1"})
    assert select.status_code == 200
    assert (out_root / body["job_id"] / "selected_variant.json").exists()

    dl = client.get(f"/download/scene-fix/{body['job_id']}")
    assert dl.status_code == 200
    with zipfile.ZipFile(io.BytesIO(dl.data)) as zf:
        names = set(zf.namelist())
    assert "manifest.json" in names
    assert "selected_variant.json" in names
    assert "images/1.png" in names
    assert "reference/source.png" in names



def test_scene_fix_revise_generated_variant(tmp_path, monkeypatch):
    out_root = tmp_path / "output"
    monkeypatch.setattr(appmod, "OUTPUT_DIR", out_root)
    monkeypatch.setattr(appmod, "APP_PASSWORD", "")
    monkeypatch.setattr(appmod, "get_channel", lambda channel_id: {
        "id": "roshia",
        "defaults": {
            "style_preset": "flat_infographic",
            "type_providers": {"diagram": "gpt-image"},
        },
    })
    monkeypatch.setattr(appmod, "resolve_channel_keys", lambda channel: {"gemini": "g", "openai": "o", "anthropic": "a"})

    calls = []
    def fake_generate(prompts, output_dir, **kwargs):
        calls.append({"prompts": prompts, "kwargs": kwargs})
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        out = []
        for p in prompts:
            fname = f"{p['index']}.png"
            _png(Path(output_dir) / fname)
            out.append({"index": p["index"], "success": True, "filename": fname, "provider": kwargs.get("provider")})
        return out

    monkeypatch.setattr(generator, "run_parallel_generation", fake_generate)

    client = appmod.app.test_client()
    created = client.post("/api/scene-fix", data={
        "channel_id": "roshia",
        "route": "diagram",
        "variant_count": "1",
        "provider": "auto",
        "sentence": "依存関係が政策判断を縛りました。",
    }).get_json()

    resp = client.post(f"/api/scene-fix/{created['job_id']}/revise", data={
        "index": "1",
        "instruction": "文字を減らして矢印を太くする",
    })

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["revision"]["filename"] == "1_rev1.png"
    assert calls[-1]["kwargs"]["edit_image_path"].endswith("/images/1.png")
    assert calls[-1]["prompts"][0]["edit_source"] is True
    manifest = json.loads((out_root / created["job_id"] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["variants"][0]["filename"] == "1_rev1.png"
    assert manifest["revisions"][0]["instruction"] == "文字を減らして矢印を太くする"
