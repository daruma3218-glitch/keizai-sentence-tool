"""Selectable channel looks, frozen into each material project's profile."""
from copy import deepcopy

TV_MARKER = "[OTONA_TV_V1]"
SUASI_MARKER = "[OTONA_SUASI_B_V1]"


def is_suasi_style(text):
    return SUASI_MARKER in (text or "")


def is_otona_style(text):
    return TV_MARKER in (text or "") or is_suasi_style(text)


def style_choices(channel):
    return [{k: s[k] for k in ("id", "name", "description")}
            for s in channel.get("styles", [])]


def resolve_style(channel, style_id=""):
    defaults = deepcopy(channel.get("defaults", {}))
    if not style_id:
        return defaults
    choice = next((s for s in channel.get("styles", []) if s["id"] == style_id), None)
    if choice is None:
        raise ValueError("このチャンネルで使えるテイストを選んでください")
    defaults.update(deepcopy(choice.get("overrides", {})))
    defaults.update(style_id=choice["id"], style_name=choice["name"])
    return defaults


SUASI_REFERENCE_INSTRUCTION = (
    "APPROVED SUASI SHACHO CHARACTER REFERENCE: The attached image is the exact mascot. "
    "Preserve his distinctive upright foot-shaped head with five toe bumps across the top, "
    "large open oval eyes, the reference's friendly smile, rosy cheeks and navy business suit. "
    "Draw Suasi Shacho exactly once as a small presenter beside the explanation, occupying "
    "roughly 12-18% of the canvas width, pointing toward its important relationship. "
    "Change his gesture to fit the explanation. Do not copy the blue square background "
    "or the creator award; omit the award unless the scene asks for it. "
    "Keep the actual mechanism, arrows and labels dominant. Do not repeat his face on icons. "
    "Use the warm restrained B look specified below for the rest of the diagram."
)
