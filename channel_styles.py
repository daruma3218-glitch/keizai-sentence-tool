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
