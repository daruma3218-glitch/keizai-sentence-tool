#!/usr/bin/env python3
"""chart/map（決定論レンダ）の個別再生成の pytest。

v3 ではグラフ/地図は AI 生成ではなく renderer で描くため、再生成も
「spec を抽出し直して renderer で描き直す」必要がある。その経路を検証する。

受け入れ基準:
- chart 行の再生成 = chart_spec を再抽出 → render_chart で描画 → ok/ファイル名を返す
- 数値が無い文（spec=None）は 422（AI生成に流れずエラー表示）
- map 行も同様に render_map で描き直す
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

import router  # noqa: E402
import renderer  # noqa: E402
import utils  # noqa: E402
import generator  # noqa: E402
import prompter  # noqa: E402
import app as appmod  # noqa: E402


def _png(out):
    Image.new("RGB", (32, 18), (255, 255, 255)).save(out)


def test_regenerate_chart_rerenders(tmp_path, monkeypatch):
    job_dir = tmp_path / "job_chart"
    (job_dir / "images").mkdir(parents=True)

    monkeypatch.setattr(utils, "get_anthropic_client", lambda key="": object())
    monkeypatch.setattr(
        router, "extract_chart_specs",
        lambda client, rows, log=None, extra_context="": {
            rows[0]["no"]: {"chart_type": "bar", "title": "t",
                            "series": [{"label": "A", "value": 6.3}]}
        },
    )

    def fake_render_chart(spec, out, theme=None):
        _png(out)
        return True
    monkeypatch.setattr(renderer, "render_chart", fake_render_chart)

    snap_row = {"no": 5, "sentence": "軍事費はGDP比6.3%。", "block_text": "出典X",
                "route": "chart", "engine": "render"}
    with appmod.app.app_context():
        resp = appmod._regenerate_render_chart(
            job_dir, 5, snap_row, {"anthropic": "k"}, {"chart_theme": {"bg": "#fff"}}, extra="")

    body = resp.get_json()
    assert body.get("ok") is True
    assert body.get("filename") == "5.png"
    assert (job_dir / "images" / "5.png").exists()


def test_regenerate_chart_without_numbers_is_422(tmp_path, monkeypatch):
    job_dir = tmp_path / "job_chart2"
    (job_dir / "images").mkdir(parents=True)

    monkeypatch.setattr(utils, "get_anthropic_client", lambda key="": object())
    monkeypatch.setattr(
        router, "extract_chart_specs",
        lambda client, rows, log=None, extra_context="": {rows[0]["no"]: None},
    )

    snap_row = {"no": 7, "sentence": "では、見ていきましょう。", "block_text": "",
                "route": "chart", "engine": "render"}
    with appmod.app.app_context():
        resp = appmod._regenerate_render_chart(
            job_dir, 7, snap_row, {"anthropic": "k"}, {}, extra="")

    assert isinstance(resp, tuple) and resp[1] == 422  # (Response, 422)


def test_regenerate_chart_uses_saved_spec_without_llm(tmp_path, monkeypatch):
    """保存済み chart_spec があり追加指示が無ければ、LLM を呼ばずにそのまま描き直す。"""
    job_dir = tmp_path / "job_saved"
    (job_dir / "images").mkdir(parents=True)

    called = {"extract": 0, "spec": None}

    def boom(*a, **k):
        called["extract"] += 1
        raise AssertionError("保存specがあるのに抽出を呼んではいけない")
    monkeypatch.setattr(router, "extract_chart_specs", boom)

    def fake_render_chart(spec, out, theme=None):
        called["spec"] = spec
        _png(out)
        return True
    monkeypatch.setattr(renderer, "render_chart", fake_render_chart)

    saved = {"chart_type": "bar", "series": [{"label": "A", "value": 6.3}]}
    snap_row = {"no": 3, "sentence": "軍事費はGDP比6.3%。", "block_text": "",
                "route": "chart", "engine": "render", "chart_spec": saved}
    with appmod.app.app_context():
        resp = appmod._regenerate_render_chart(
            job_dir, 3, snap_row, {"anthropic": "k"}, {}, extra="")

    body = resp.get_json()
    assert body.get("ok") is True
    assert called["extract"] == 0           # LLM 抽出は呼ばれていない
    assert called["spec"] == saved          # 保存 spec をそのまま描いた


def test_regenerate_chart_instruction_forces_reextract(tmp_path, monkeypatch):
    """追加指示があれば保存 spec を使わず抽出し直す（数値・体裁を変えられる）。"""
    job_dir = tmp_path / "job_instr"
    (job_dir / "images").mkdir(parents=True)

    new_spec = {"chart_type": "pie", "series": [{"label": "X", "value": 50}]}
    seen_ctx = {}

    def fake_extract(client, rows, log=None, extra_context=""):
        seen_ctx["block_text"] = rows[0].get("block_text", "")
        return {rows[0]["no"]: new_spec}
    monkeypatch.setattr(router, "extract_chart_specs", fake_extract)
    monkeypatch.setattr(utils, "get_anthropic_client", lambda key="": object())

    rendered = {}
    def fake_render_chart(spec, out, theme=None):
        rendered["spec"] = spec
        _png(out)
        return True
    monkeypatch.setattr(renderer, "render_chart", fake_render_chart)

    saved = {"chart_type": "bar", "series": [{"label": "A", "value": 1}]}
    snap_row = {"no": 4, "sentence": "ある文。", "block_text": "段落。",
                "route": "chart", "engine": "render", "chart_spec": saved}
    with appmod.app.app_context():
        resp = appmod._regenerate_render_chart(
            job_dir, 4, snap_row, {"anthropic": "k"}, {}, extra="円グラフで X=50")

    body = resp.get_json()
    assert body.get("ok") is True
    assert rendered["spec"] == new_spec                  # 抽出し直した spec で描いた
    assert "円グラフで X=50" in seen_ctx["block_text"]   # 指示が文脈に入っている


def test_regenerate_map_rerenders(tmp_path, monkeypatch):
    job_dir = tmp_path / "job_map"
    (job_dir / "images").mkdir(parents=True)

    monkeypatch.setattr(utils, "get_anthropic_client", lambda key="": object())
    monkeypatch.setattr(
        router, "extract_map_specs",
        lambda client, rows, log=None: {rows[0]["no"]: {"map_type": "highlight",
                                                        "focus_countries": ["RUS"]}},
    )

    def fake_render_map(spec, out, theme=None):
        _png(out)
        return True
    monkeypatch.setattr(renderer, "render_map", fake_render_map)

    snap_row = {"no": 9, "sentence": "ソ連は14か国と国境を接していた。", "block_text": "",
                "route": "map", "engine": "render"}
    with appmod.app.app_context():
        resp = appmod._regenerate_render_map(
            job_dir, 9, snap_row, {"anthropic": "k"}, {}, extra="")

    body = resp.get_json()
    assert body.get("ok") is True and body.get("filename") == "9.png"
    assert (job_dir / "images" / "9.png").exists()


def test_regenerate_chart_can_force_diagram_ai(tmp_path, monkeypatch):
    """chart(render) 行でも force_route=diagram ならAI図解として再生成できる。"""
    out_root = tmp_path / "output"
    job_dir = out_root / "job_force"
    (job_dir / "images").mkdir(parents=True)
    monkeypatch.setattr(appmod, "OUTPUT_DIR", out_root)
    monkeypatch.setattr(appmod, "APP_PASSWORD", "")
    monkeypatch.setattr(
        appmod,
        "get_channel",
        lambda channel_id: {"defaults": {
            "user_instructions": "CHANNEL BASE STYLE: serious educational infographic.",
            "worldview_desc": "CHANNEL WORLDVIEW: muted geopolitics explainer.",
        }},
    )
    monkeypatch.setattr(appmod, "resolve_channel_keys", lambda channel: {"anthropic": "k", "gemini": "g", "openai": ""})

    (job_dir / "job.json").write_text(
        '{"channel_id":"default","provider":"nanobanana","style_preset":"flat_infographic"}',
        encoding="utf-8",
    )
    (job_dir / "prompts.json").write_text('{"rows":[]}', encoding="utf-8")
    (job_dir / "rows_progress.json").write_text(
        '{"rows":[{"no":12,"sentence":"天然ガス価格は29ドルから430ドルです。",'
        '"block_text":"価格比較","route":"chart","engine":"render","status":"ok",'
        '"chart_spec":{"chart_type":"bar","series":[{"label":"A","value":29}]}}]}',
        encoding="utf-8",
    )

    monkeypatch.setattr(utils, "get_anthropic_client", lambda key="": object())
    seen = {}

    def fake_prompts(client, rows, **kwargs):
        seen["kwargs"] = kwargs
        return [{
            "no": rows[0]["no"],
            "prompt": "Create a clear diagram with two boxes and arrows, no chart.",
            "type": "diagram",
            "route": "diagram",
            "allowed_terms": ["29ドル", "430ドル"],
            "character": False,
            "sentence": rows[0]["sentence"],
        }]

    monkeypatch.setattr(
        prompter,
        "generate_all_prompts",
        fake_prompts,
    )

    calls = {}
    def fake_generate(prompts, output_dir, **kwargs):
        calls["entry"] = prompts[0]
        _png(Path(output_dir) / "12.png")
        return [{"success": True, "filename": "12.png"}]
    monkeypatch.setattr(generator, "run_parallel_generation", fake_generate)

    with appmod.app.test_request_context(
        "/api/regenerate/job_force/12",
        method="POST",
        data={"force_route": "diagram"},
    ):
        resp = appmod.api_regenerate("job_force", 12)

    body = resp.get_json()
    assert body.get("ok") is True
    assert body.get("route") == "diagram"
    assert calls["entry"]["type"] == "diagram"
    assert "CHANNEL BASE STYLE" in seen["kwargs"]["user_instructions"]
    assert "forced route/type" in seen["kwargs"]["user_instructions"]
    assert seen["kwargs"]["worldview_desc"] == "CHANNEL WORLDVIEW: muted geopolitics explainer."
    snap = appmod.load_json(job_dir / "rows_progress.json", {"rows": []})["rows"][0]
    assert snap["route"] == "diagram"
    assert snap["engine"] == "ai"


def test_regenerate_realphoto_passes_watermark_flag(tmp_path, monkeypatch):
    """realphoto の個別再生成でも右上「イメージ」焼き込みを有効にする。"""
    out_root = tmp_path / "output"
    job_dir = out_root / "job_realphoto"
    (job_dir / "images").mkdir(parents=True)
    monkeypatch.setattr(appmod, "OUTPUT_DIR", out_root)
    monkeypatch.setattr(appmod, "APP_PASSWORD", "")
    monkeypatch.setattr(
        appmod,
        "get_channel",
        lambda channel_id: {"defaults": {"realphoto_watermark": True}},
    )
    monkeypatch.setattr(appmod, "resolve_channel_keys", lambda channel: {"gemini": "g", "openai": ""})

    (job_dir / "job.json").write_text(
        '{"channel_id":"roshia","provider":"nanobanana","style_preset":"flat_infographic"}',
        encoding="utf-8",
    )
    (job_dir / "prompts.json").write_text(
        '{"rows":[{"no":14,"prompt":"Photorealistic documentary photograph.",'
        '"type":"realphoto","route":"realphoto","sentence":"会談の場面です。"}]}',
        encoding="utf-8",
    )
    (job_dir / "rows_progress.json").write_text(
        '{"rows":[{"no":14,"sentence":"会談の場面です。","route":"realphoto","engine":"ai","status":"ok"}]}',
        encoding="utf-8",
    )
    _png(job_dir / "images" / "14.png")

    seen = {}

    def fake_generate(prompts, output_dir, **kwargs):
        seen["entry"] = prompts[0]
        seen["kwargs"] = kwargs
        _png(Path(output_dir) / "14.png")
        return [{"success": True, "filename": "14.png"}]

    monkeypatch.setattr(generator, "run_parallel_generation", fake_generate)

    with appmod.app.test_request_context(
        "/api/regenerate/job_realphoto/14",
        method="POST",
        data={},
    ):
        resp = appmod.api_regenerate("job_realphoto", 14)

    body = resp.get_json()
    assert body.get("ok") is True
    assert seen["entry"]["type"] == "realphoto"
    assert seen["entry"]["edit_source"] is True
    assert seen["kwargs"]["edit_image_path"] == str(job_dir / "images" / "14.png")
    assert seen["kwargs"]["realphoto_watermark"] is True


def test_regenerate_generation_exception_marks_snapshot_failed(tmp_path, monkeypatch):
    """AI再生成が例外で落ちても rows_progress は failed に戻し、生成中で固めない。"""
    out_root = tmp_path / "output"
    job_dir = out_root / "job_fail"
    (job_dir / "images").mkdir(parents=True)
    monkeypatch.setattr(appmod, "OUTPUT_DIR", out_root)
    monkeypatch.setattr(appmod, "APP_PASSWORD", "")
    monkeypatch.setattr(appmod, "get_channel", lambda channel_id: {"defaults": {}})
    monkeypatch.setattr(appmod, "resolve_channel_keys", lambda channel: {"gemini": "g", "openai": ""})

    (job_dir / "job.json").write_text(
        '{"channel_id":"default","provider":"nanobanana","style_preset":"flat_infographic"}',
        encoding="utf-8",
    )
    (job_dir / "prompts.json").write_text(
        '{"rows":[{"no":15,"prompt":"Create a diagram.",'
        '"type":"diagram","route":"diagram","sentence":"失敗確認の文です。"}]}',
        encoding="utf-8",
    )
    (job_dir / "rows_progress.json").write_text(
        '{"rows":[{"no":15,"sentence":"失敗確認の文です。","route":"diagram","engine":"ai","status":"ok"}]}',
        encoding="utf-8",
    )

    def fail_generate(*args, **kwargs):
        raise RuntimeError("provider timeout")

    monkeypatch.setattr(generator, "run_parallel_generation", fail_generate)

    with appmod.app.test_request_context(
        "/api/regenerate/job_fail/15",
        method="POST",
        data={},
    ):
        resp, status = appmod.api_regenerate("job_fail", 15)

    assert status == 500
    assert "provider timeout" in resp.get_json()["error"]
    snap = appmod.load_json(job_dir / "rows_progress.json", {"rows": []})["rows"][0]
    assert snap["status"] == "failed"
    assert snap["verify_issue"] is True
    assert "provider timeout" in snap["error"]


def test_regenerate_can_force_skip_without_generation(tmp_path, monkeypatch):
    out_root = tmp_path / "output"
    job_dir = out_root / "job_skip"
    (job_dir / "images").mkdir(parents=True)
    monkeypatch.setattr(appmod, "OUTPUT_DIR", out_root)
    monkeypatch.setattr(appmod, "APP_PASSWORD", "")

    (job_dir / "job.json").write_text(
        '{"channel_id":"default","provider":"nanobanana","style_preset":"flat_infographic"}',
        encoding="utf-8",
    )
    (job_dir / "prompts.json").write_text('{"rows":[]}', encoding="utf-8")
    (job_dir / "rows_progress.json").write_text(
        '{"rows":[{"no":13,"sentence":"つなぎの一文です。","route":"diagram","engine":"ai","status":"ok"}]}',
        encoding="utf-8",
    )

    with appmod.app.test_request_context(
        "/api/regenerate/job_skip/13",
        method="POST",
        data={"force_route": "skip"},
    ):
        resp = appmod.api_regenerate("job_skip", 13)

    body = resp.get_json()
    assert body.get("ok") is True
    assert body.get("skipped") is True
    snap = appmod.load_json(job_dir / "rows_progress.json", {"rows": []})["rows"][0]
    assert snap["route"] == "skip"
    assert snap["status"] == "skipped"
    assert snap["engine"] == "none"
