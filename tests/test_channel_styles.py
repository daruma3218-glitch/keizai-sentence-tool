from pathlib import Path

from app import get_channel
from pipeline import SentencePipeline, VALID_STYLES
from prompter import _build_user_block, _limit_allowed_terms
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
    assert defaults["web_image_count"] == 60
    assert defaults["web_search_profile"] == "primary_media"
    assert "一次情報" in defaults["user_instructions"]
    assert "realphoto" in defaults["user_instructions"]


def test_roshia_channel_disables_charts_and_blocks_japan_leakage():
    defaults = get_channel("roshia").get("defaults", {})
    assert defaults["allow_charts"] is False
    assert defaults["intro_visual_boost"] == 10
    assert "日本地図" in defaults["user_instructions"]
    assert "円マーク" in defaults["user_instructions"]
    assert "冒頭10文" in defaults["user_instructions"]


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
    assert routes[2]["route"] == "map"
    assert routes[3]["route"] == "web_photo"
    assert routes[4]["route"] == "diagram"


def test_allowed_terms_are_limited_to_reduce_keyword_lists():
    sentence = "ロシアのGDPは2025年に6.3%低下し、輸出額は430ドル相当で、ベラルーシと中国にも影響した。"
    terms = ["ロシア", "GDP", "2025年", "6.3%", "輸出額", "430ドル", "ベラルーシ", "中国"]
    limited = _limit_allowed_terms(terms, sentence)
    assert len(limited) <= 4
    assert len(set(limited)) == len(limited)
    assert any(t in limited for t in ("6.3%", "430ドル", "2025年"))
