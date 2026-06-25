from pathlib import Path

from app import get_channel
from pipeline import SentencePipeline, VALID_STYLES
from prompter import (
    _build_user_block,
    _fallback_prompt_for_row,
    _limit_allowed_terms,
    _normalize_diagram_blueprint,
)
from generator import _build_full_prompt


def test_soviet_propaganda_style_is_available_for_all_channels(tmp_path):
    assert "soviet_propaganda" in VALID_STYLES

    for channel_id in ("keizai", "roshia", "seikou"):
        pipe = SentencePipeline(
            manuscript_text="x" * 200,
            output_dir=tmp_path / channel_id,
            channel_id=channel_id,
            style_preset="soviet_propaganda",
        )
        assert pipe.style_preset == "soviet_propaganda"


def test_soviet_propaganda_reaches_prompt_layers():
    user_block = _build_user_block("", "soviet_propaganda")
    final_prompt = _build_full_prompt(
        "Show books and a globe as educational symbols.",
        "illustration",
        allowed_terms=[],
        style_preset="soviet_propaganda",
    )

    assert "ソ連プロパガンダ風" in user_block
    assert "historical Soviet-era educational poster style" in final_prompt
    assert "Do NOT include weapons" in final_prompt


def test_channel_character_refs_point_to_existing_files_or_are_blank():
    root = Path(__file__).resolve().parents[1]
    for channel_id in ("keizai", "roshia", "seikou"):
        defaults = get_channel(channel_id).get("defaults", {})
        ref = defaults.get("character_ref", "")
        assert not ref or (root / ref).exists()


def test_seikou_channel_uses_dedicated_api_prefix():
    channel = get_channel("seikou")
    assert channel["name"] == "成功の法則"
    assert channel["api_env_prefix"] == "SEIKOU"
    defaults = channel.get("defaults", {})
    assert defaults["provider"] == "nanobanana"
    assert defaults["chart_engine"] == "render"
    assert defaults["map_engine"] == "render"
    assert defaults["allow_maps"] is False
    assert defaults["web_image_count"] == 60
    assert defaults["web_search_profile"] == "primary_media"
    assert "一次情報" in defaults["user_instructions"]
    assert "realphoto" in defaults["user_instructions"]


def test_roshia_channel_disables_charts_and_blocks_japan_leakage():
    defaults = get_channel("roshia").get("defaults", {})
    assert defaults["allow_charts"] is False
    assert defaults["allow_maps"] is False
    assert defaults["intro_visual_boost"] == 10
    assert defaults["map_route_limit"] == 8
    assert defaults["realistic_route_min"] == 35
    assert defaults["web_image_count"] == 45
    assert defaults["web_search_profile"] == "primary_media"
    assert defaults["no_image_text"] is False
    assert "日本地図" in defaults["user_instructions"]
    assert "円マーク" in defaults["user_instructions"]
    assert "冒頭10文" in defaults["user_instructions"]
    assert "YouTubeの教養チャンネル" in defaults["user_instructions"]
    assert "位置関係図解" in defaults["user_instructions"]
    assert "短いラベル付きインフォグラフィック" in defaults["user_instructions"]
    assert "1〜4語まで使用してよい" in defaults["user_instructions"]
    assert "可愛い" in defaults["user_instructions"]
    assert "warm retro hand-drawn" not in defaults["worldview_desc"]
    assert "symbolic flat infographics" in defaults["worldview_desc"]
    assert "ライトグレイッシュ" in defaults["user_instructions"]
    assert "Do not use black, dark navy" in defaults["worldview_desc"]
    assert defaults["chart_theme"]["bg"] == "#D9E1E8"


def test_intro_visual_boost_prefers_realistic_opening(tmp_path):
    pipe = SentencePipeline(
        "dummy",
        tmp_path,
        intro_visual_boost=3,
        verify_diagrams=False,
    )
    rows = [
        {"no": 1, "sentence": "では、ベラルーシとロシアの関係を見ていきましょう。", "block_text": ""},
        {"no": 2, "sentence": "ロシア軍はベラルーシ経由でウクライナへ進軍しました。", "block_text": ""},
        {"no": 3, "sentence": "ルカシェンコ大統領は会談で支援を表明しました。", "block_text": ""},
        {"no": 4, "sentence": "その背景には政治制度の問題があります。", "block_text": ""},
    ]
    routes = {
        1: {"route": "skip", "importance": 1},
        2: {"route": "diagram", "importance": 3},
        3: {"route": "diagram", "importance": 3},
        4: {"route": "diagram", "importance": 3},
    }
    changed = pipe._apply_intro_visual_boost(rows, routes)
    assert changed == 3
    assert routes[1]["route"] == "realphoto"
    assert routes[2]["route"] == "realphoto"
    assert routes[3]["route"] == "web_photo"
    assert routes[4]["route"] == "diagram"


def test_intro_visual_boost_uses_diagram_for_explicit_geography_when_maps_disabled(tmp_path):
    pipe = SentencePipeline(
        "dummy",
        tmp_path,
        intro_visual_boost=1,
        allow_maps=False,
        verify_diagrams=False,
    )
    rows = [
        {"no": 1, "sentence": "国境線と領土の位置関係を地図で確認します。", "block_text": ""},
    ]
    routes = {1: {"route": "diagram", "importance": 3}}
    changed = pipe._apply_intro_visual_boost(rows, routes)
    assert changed == 1
    assert routes[1]["route"] == "diagram"


def test_disable_map_routes_converts_maps_to_relationship_diagrams(tmp_path):
    pipe = SentencePipeline(
        "dummy",
        tmp_path,
        allow_maps=False,
        verify_diagrams=False,
    )
    rows = [
        {"no": 1, "sentence": "ロシア軍はベラルーシ経由で進軍しました。", "block_text": ""},
    ]
    routes = {1: {"route": "map", "importance": 3}}
    changed = pipe._disable_map_routes(rows, routes)
    assert changed == 1
    assert routes[1]["route"] == "diagram"
    assert routes[1]["engine"] == "ai"
    assert "位置関係" in routes[1]["reason"]
    assert "no map outlines" in routes[1]["visual_hint"]


def test_map_route_limit_converts_extra_maps_to_realphoto(tmp_path):
    pipe = SentencePipeline(
        "dummy",
        tmp_path,
        allow_maps=True,
        map_route_limit=2,
        verify_diagrams=False,
    )
    rows = [
        {"no": 1, "sentence": "国境線と領土の位置関係を地図で確認します。", "block_text": ""},
        {"no": 2, "sentence": "ロシア軍はベラルーシ経由で進軍しました。", "block_text": ""},
        {"no": 3, "sentence": "欧州とNATOの関係が変化しました。", "block_text": ""},
    ]
    routes = {
        1: {"route": "map", "importance": 3},
        2: {"route": "map", "importance": 3},
        3: {"route": "map", "importance": 1},
    }
    changed = pipe._limit_map_routes(rows, routes)
    assert changed == 1
    assert sum(1 for rt in routes.values() if rt["route"] == "map") == 2
    assert routes[3]["route"] == "realphoto"


def test_no_image_text_clears_allowed_terms(tmp_path):
    pipe = SentencePipeline(
        "dummy",
        tmp_path,
        no_image_text=True,
        verify_diagrams=False,
    )
    rows = [
        {"no": 1, "allowed_terms": ["ロシア", "NATO"]},
        {"no": 2, "allowed_terms": []},
    ]
    changed = pipe._remove_image_text_terms(rows)
    assert changed == 1
    assert rows[0]["allowed_terms"] == []
    assert rows[1]["allowed_terms"] == []


def test_realistic_route_boost_adds_web_and_realphoto(tmp_path):
    pipe = SentencePipeline(
        "dummy",
        tmp_path,
        realistic_route_min=3,
        verify_diagrams=False,
    )
    rows = [
        {"no": 1, "sentence": "ルカシェンコ大統領は会談で支援を表明しました。", "block_text": ""},
        {"no": 2, "sentence": "パイプラインとエネルギー供給が経済を支えました。", "block_text": ""},
        {"no": 3, "sentence": "都市の軍事施設が重要な意味を持ちました。", "block_text": ""},
        {"no": 4, "sentence": "国境線と領土の位置関係を地図で確認します。", "block_text": ""},
    ]
    routes = {
        1: {"route": "diagram", "importance": 3},
        2: {"route": "diagram", "importance": 3},
        3: {"route": "illustration", "importance": 3},
        4: {"route": "map", "importance": 3},
    }
    changed = pipe._boost_realistic_routes(rows, routes)
    assert changed == 3
    assert routes[1]["route"] == "web_photo"
    assert routes[2]["route"] == "realphoto"
    assert routes[3]["route"] == "web_photo"
    assert routes[4]["route"] == "map"


def test_allowed_terms_are_limited_to_reduce_keyword_lists():
    sentence = "ロシアのGDPは2025年に6.3%低下し、輸出額は430ドル相当で、ベラルーシと中国にも影響した。"
    terms = ["ロシア", "GDP", "2025年", "6.3%", "輸出額", "430ドル", "ベラルーシ", "中国"]
    limited = _limit_allowed_terms(terms, sentence)
    assert len(limited) <= 4
    assert len(set(limited)) == len(limited)
    assert any(t in limited for t in ("6.3%", "430ドル", "2025年"))


def test_diagram_prompts_require_readable_visual_argument():
    user_block = _build_user_block("", "flat_infographic")
    final_prompt = _build_full_prompt(
        "Visual goal: explain why Belarus depends on Russia. Reading path: left to right, three connected elements with arrows.",
        "diagram",
        allowed_terms=["ベラルーシ", "ロシア"],
        style_preset="flat_infographic",
    )
    fallback = _fallback_prompt_for_row({
        "no": 1,
        "route": "diagram",
        "sentence": "ベラルーシはロシアへの経済依存を深めました。",
    })

    assert "読む順番" in user_block
    assert "#D9E1E8" in user_block
    assert "#1B365D" in user_block
    assert "角丸シェイプ" in user_block
    assert "labels and arrows in order" in final_prompt
    assert "3-5 connected elements" in final_prompt
    assert "#D9E1E8" in final_prompt
    assert "#1B365D" in final_prompt
    assert "rounded backing shapes" in final_prompt
    assert "one visual goal" in fallback["prompt"]
    assert "reading path" in fallback["prompt"]
    assert fallback["diagram_blueprint"]["visual_goal"]
    assert "Diagram blueprint to follow exactly" in fallback["prompt"]


def test_diagram_blueprint_filters_labels_to_source_sentence():
    row = {
        "no": 1,
        "route": "diagram",
        "sentence": "ベラルーシはロシアへの経済依存を深めました。",
    }
    bp = _normalize_diagram_blueprint(
        {
            "visual_goal": "依存関係を示す",
            "structure": "dependency",
            "reading_path": "left-to-right",
            "elements": ["ベラルーシ", "ロシア", "経済依存"],
            "relationships": ["ベラルーシからロシアへ依存の矢印"],
            "labels": ["ベラルーシ", "ロシア", "NATO"],
            "forbidden": ["キーワード羅列"],
        },
        row,
        allowed_terms=["ベラルーシ", "ロシア", "経済依存"],
    )
    assert bp["structure"] == "dependency"
    assert bp["reading_path"] == "left-to-right"
    assert "Template:" in bp["template"]
    assert "ベラルーシ" in bp["labels"]
    assert "ロシア" in bp["labels"]
    assert "NATO" not in bp["labels"]


def test_diagram_context_is_attached_only_to_diagram_rows(tmp_path):
    pipe = SentencePipeline("dummy", tmp_path, verify_diagrams=False)
    all_rows = [
        {"no": 1, "sentence": "前の文です。", "route": "realphoto"},
        {"no": 2, "sentence": "ベラルーシはロシアへの依存を深めました。", "route": "diagram"},
        {"no": 3, "sentence": "次の文です。", "route": "diagram"},
    ]
    prompt_rows = [all_rows[0], all_rows[1]]
    enriched = pipe._attach_diagram_context(prompt_rows, all_rows, window=1)
    assert "diagram_context" not in enriched[0]
    assert "前後#1" in enriched[1]["diagram_context"]
    assert "対象#2" in enriched[1]["diagram_context"]
    assert "前後#3" in enriched[1]["diagram_context"]
