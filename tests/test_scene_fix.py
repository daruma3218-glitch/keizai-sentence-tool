import json
import sys
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
    manifest = out_root / body["job_id"] / "manifest.json"
    assert manifest.exists()
    saved = json.loads(manifest.read_text(encoding="utf-8"))
    assert saved["sentence"].startswith("ベラルーシ")
