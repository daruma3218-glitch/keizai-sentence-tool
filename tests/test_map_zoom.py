#!/usr/bin/env python3
"""地図の自動ズーム優先化（改修④）の pytest。

受け入れ基準:
- 広域プリセット(world)指定でも、対象国群が十分小さければ自動ズーム
  （ロシアは日付変更線またぎ→他国近傍の西側だけ取り込む）
- europe プリセットで十分収まる場合はプリセット尊重（ズームしない）
- 対象が1国だけ（例: ロシア単体）はプリセット尊重（大国クロップ事故防止）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import renderer  # noqa: E402


def _geo():
    g = renderer._load_geo()
    assert g, "GeoJSON アセットが読める前提"
    return g


def test_world_preset_zooms_to_corridor():
    geo = _geo()
    ext = renderer._resolve_extent({"extent": "world"}, geo, ["RUS", "BLR", "DEU"])
    assert ext != renderer._EXTENTS["world"], "worldのままではダメ（自動ズームすべき）"
    x0, x1, y0, y1 = ext
    assert x1 < 130, f"極東ロシアまで含めない（x1={x1}）"
    # ドイツ(約5-15E)とベラルーシ(約23-32E)が範囲内
    assert x0 <= 5 and x1 >= 32
    assert y0 <= 47 and y1 >= 56


def test_europe_preset_respected_when_fits():
    geo = _geo()
    ext = renderer._resolve_extent({"extent": "europe"}, geo, ["RUS", "BLR", "DEU"])
    assert ext == renderer._EXTENTS["europe"], "十分収まるプリセットはそのまま使う"


def test_single_giant_country_keeps_preset():
    geo = _geo()
    ext = renderer._resolve_extent({"extent": "world"}, geo, ["RUS"])
    assert ext == renderer._EXTENTS["world"], "ロシア単体でズームすると本体が切れるのでプリセット尊重"


def test_render_map_world_route_still_renders(tmp_path):
    theme = {"bg": "#FBFAF7", "main": "#1E3A5F", "accent": "#B22222",
             "grid": "#E5E7EB", "text": "#1F2937", "font": "NotoSansJP"}
    spec = {"map_type": "route", "extent": "world",
            "title": "ロシアから欧州へのエネルギーパイプライン",
            "focus_countries": ["RUS", "BLR", "DEU"],
            "arrows": [{"from": "RUS", "to": "BLR", "label": "輸送"},
                       {"from": "BLR", "to": "DEU", "label": "通過"}]}
    out = tmp_path / "zoom.png"
    assert renderer.render_map(spec, out, theme=theme) is True
