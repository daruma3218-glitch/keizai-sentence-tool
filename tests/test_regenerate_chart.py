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
