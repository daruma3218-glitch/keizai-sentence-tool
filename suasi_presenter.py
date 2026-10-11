"""Place the existing Suasi artwork without asking an image model to redraw him."""
import hashlib
import json
from io import BytesIO
from pathlib import Path

from PIL import Image, PngImagePlugin


ART_PATH = Path(__file__).resolve().parent / "references/suasi-good-original.png"
ART_SHA256 = "eaf4886892f21ed067fa8700ed7aaa72480e3a22f68e021f44fb3f5c023c77ee"
RECIPE_KEY = "studio_suasi_presenter"
RECIPE_VERSION = 2
BACKGROUND = (255, 251, 242, 255)

BOARD_INSTRUCTION = (
    "SUASI B DIAGRAM LAYER ONLY: A separate renderer will place the exact existing "
    "Suasi artwork beside this diagram. Do NOT draw, reinterpret or include Suasi, "
    "a host, a mascot, a character portrait or any substitute presenter in this image. "
    "Do not reserve a presenter area inside the diagram; use the whole image for its "
    "mechanism, labels and arrows. Use a uniform warm ivory background (#FFFBF2), "
    "rounded dark outlines, pale blue, cream and small gold accents. No outer frame. "
    "If an edit source contains a presenter, REMOVE that presenter and retain the "
    "meaning, exact text, conditions, comparison direction and diagram relationships. "
    "This layer-only rule overrides any older instruction to draw or preserve a mascot."
)


def load_artwork():
    try:
        data = ART_PATH.read_bytes()
        if hashlib.sha256(data).hexdigest() != ART_SHA256:
            raise ValueError("original artwork hash mismatch")
        with Image.open(BytesIO(data)) as image:
            image.load()
            if image.mode != "RGBA" or not image.getchannel("A").getbbox():
                raise ValueError("original artwork needs transparency")
        return data
    except (OSError, ValueError) as exc:
        raise ValueError("すあし社長の承認済み原画を確認できません。生成を開始できません") from exc


def diagram_edit_source(data):
    """An edit of our output gets only the diagram, never a redraw of the artwork."""
    with Image.open(BytesIO(data)) as image:
        raw = image.info.get(RECIPE_KEY)
        if not raw:
            # V1 images have an AI-drawn mascot; BOARD_INSTRUCTION removes it.
            return data
        try:
            recipe = json.loads(raw)
            box = recipe["diagram_box"]
            if (recipe.get("version") != RECIPE_VERSION or len(box) != 4
                    or not all(isinstance(v, int) for v in box)
                    or not (0 <= box[0] < box[2] <= image.width
                            and 0 <= box[1] < box[3] <= image.height)):
                raise ValueError("invalid diagram bounds")
        except (TypeError, KeyError, ValueError) as exc:
            raise ValueError("保存画像の図解範囲を確認できません") from exc
        output = BytesIO()
        image.crop(box).convert("RGB").save(output, format="PNG")
        return output.getvalue()


def compose_presenter(path, artwork):
    """Keep the diagram and the original in disjoint areas, so neither hides text."""
    path = Path(path)
    with Image.open(path) as image:
        board = image.convert("RGBA")
    width, height = board.size
    if width < 64 or height < 36:
        raise ValueError("図解の画像サイズが小さすぎます")
    # Scaling is uniform: the original character's head/body/eye ratios never change.
    board_width = round(width * 0.79)
    board_height = max(1, round(height * 0.79))
    board = board.resize((board_width, board_height), Image.Resampling.LANCZOS)
    board_y = (height - board_height) // 2
    canvas = Image.new("RGBA", (width, height), BACKGROUND)
    canvas.alpha_composite(board, (0, board_y))
    with Image.open(BytesIO(artwork)) as original:
        character = original.crop(original.getchannel("A").getbbox())
    scale = min(width * 0.17 / character.width, height * 0.43 / character.height)
    character = character.resize((max(1, round(character.width * scale)),
                                  max(1, round(character.height * scale))), Image.Resampling.LANCZOS)
    x = width - round(width * 0.02) - character.width
    y = height - round(height * 0.04) - character.height
    canvas.alpha_composite(character, (x, y))
    recipe = {"version": RECIPE_VERSION, "art_sha256": ART_SHA256,
              "diagram_box": [0, board_y, board_width, board_y + board_height],
              "character_box": [x, y, x + character.width, y + character.height]}
    meta = PngImagePlugin.PngInfo()
    meta.add_text(RECIPE_KEY, json.dumps(recipe, separators=(",", ":")))
    canvas.convert("RGB").save(path, format="PNG", pnginfo=meta)
    return recipe
