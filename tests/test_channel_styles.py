from pathlib import Path

from app import get_channel
from pipeline import SentencePipeline, VALID_STYLES
from prompter import _build_user_block
from generator import _build_full_prompt


def test_soviet_propaganda_style_is_available_for_all_channels(tmp_path):
    assert "soviet_propaganda" in VALID_STYLES

    for channel_id in ("keizai", "roshia"):
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
    for channel_id in ("keizai", "roshia"):
        defaults = get_channel(channel_id).get("defaults", {})
        ref = defaults.get("character_ref", "")
        assert not ref or (root / ref).exists()
