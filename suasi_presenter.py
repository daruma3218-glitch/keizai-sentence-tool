"""Place the existing Suasi artwork without asking an image model to redraw him."""
import hashlib
import json
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageOps, PngImagePlugin


ART_PATH = Path(__file__).resolve().parent / "references/suasi-good-original.png"
ART_SHA256 = "eaf4886892f21ed067fa8700ed7aaa72480e3a22f68e021f44fb3f5c023c77ee"
RECIPE_KEY = "studio_suasi_presenter"
RECIPE_VERSION = 3
BACKGROUND = (255, 251, 242, 255)
POSE_DIR = ART_PATH.parent / "suasi-poses"
DIAGRAM_RIGHT = 0.865
CHARACTER_WIDTH = 0.115
CHARACTER_HEIGHT = 0.31

BOARD_INSTRUCTION = (
    "SUASI B DIAGRAM LAYER ONLY: A separate renderer will place the exact existing "
    "Suasi artwork beside this diagram. Do NOT draw, reinterpret or include Suasi, "
    "a host, a mascot, a character portrait or any substitute presenter in this image. "
    "Do not reserve a presenter area inside the diagram; use the whole image for its "
    "mechanism, labels and arrows. Make the diagram itself large, with readable labels "
    "and only a small safety margin; no large empty outer bands. "
    "Use a uniform warm ivory background (#FFFBF2), "
    "rounded dark outlines, pale blue, cream and small gold accents. No outer frame. "
    "If an edit source contains a presenter, REMOVE that presenter and retain the "
    "meaning, exact text, conditions, comparison direction and diagram relationships. "
    "This layer-only rule overrides any older instruction to draw or preserve a mascot."
)


def pose_catalog():
    catalog = json.loads((POSE_DIR / "catalog.json").read_text(encoding="utf-8"))
    from suasi_pose import POSE_NAMES
    if (catalog.get("schema_version") != 1 or catalog.get("identity_master_sha256") != ART_SHA256
            or set(catalog.get("poses", {})) != set(POSE_NAMES)):
        raise ValueError("すあし社長のポーズ台帳を確認できません")
    return catalog["poses"]


def load_artwork(pose_id="good"):
    try:
        if pose_id == "good":
            path, expected_sha = ART_PATH, ART_SHA256
        else:
            pose = pose_catalog()[pose_id]
            if pose["file"] != pose_id + ".png":
                raise ValueError("invalid artwork path")
            path, expected_sha = POSE_DIR / pose["file"], pose["sha256"]
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_sha:
            raise ValueError("original artwork hash mismatch")
        with Image.open(BytesIO(data)) as image:
            image.load()
            if (image.mode != "RGBA" or not image.getchannel("A").getbbox()
                    or image.getchannel("A").getextrema()[0] != 0):
                raise ValueError("original artwork needs transparency")
        return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("すあし社長の承認済み原画を確認できません。生成を開始できません") from exc


def load_artworks():
    # Validate before any paid image call, including saved projects.
    return {pose: load_artwork(pose) for pose in ("good", *pose_catalog())}


def read_recipe(data):
    with Image.open(BytesIO(data)) as image:
        raw = image.info.get(RECIPE_KEY)
        if not raw:
            return None
        try:
            recipe = json.loads(raw)
            box = recipe["diagram_box"]
            if (recipe.get("version") not in (2, RECIPE_VERSION) or len(box) != 4
                    or not all(type(v) is int for v in box)
                    or not (0 <= box[0] < box[2] <= image.width
                            and 0 <= box[1] < box[3] <= image.height)):
                raise ValueError("invalid diagram bounds")
            if recipe.get("version") == RECIPE_VERSION:
                pose_id = recipe["pose_id"]
                expected = ART_SHA256 if pose_id == "good" else pose_catalog()[pose_id]["sha256"]
                char = recipe["character_box"]
                if (recipe.get("art_sha256") != expected or len(char) != 4
                        or not all(type(v) is int for v in char)
                        or not (box[2] < char[0] < char[2] <= image.width
                                and 0 <= char[1] < char[3] <= image.height)):
                    raise ValueError("invalid presenter bounds")
            return recipe
        except (TypeError, KeyError, ValueError) as exc:
            raise ValueError("保存画像の図解範囲を確認できません") from exc


def diagram_edit_source(data):
    """An edit of our output gets only the diagram, never a redraw of the artwork."""
    recipe = read_recipe(data)
    if recipe is None:
        # V1 images have an AI-drawn mascot; BOARD_INSTRUCTION removes it.
        return data
    with Image.open(BytesIO(data)) as image:
        output = BytesIO()
        image.crop(recipe["diagram_box"]).convert("RGB").save(output, format="PNG")
        return output.getvalue()


def trim_diagram(board):
    """Trim only near-uniform ivory/white outer space; retain a safety border."""
    rgb = board.convert("RGB")
    corners = [rgb.getpixel(p) for p in ((0, 0), (rgb.width-1, 0),
                (0, rgb.height-1), (rgb.width-1, rgb.height-1))]
    # Photos, coloured backgrounds or art extending to a corner stay intact.
    if any(min(c) < 225 for c in corners) or any(
            max(c[i] for c in corners)-min(c[i] for c in corners) > 12 for i in range(3)):
        return board, (0, 0, board.width, board.height)
    color = tuple(round(sum(c[i] for c in corners)/4) for i in range(3))
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, color))
    mask = ImageChops.lighter(ImageChops.lighter(*diff.split()[:2]), diff.split()[2]).point(lambda v: 255 if v > 12 else 0)
    box = mask.getbbox()
    if not box:
        return board, (0, 0, board.width, board.height)
    # Tiny marks alone must not be enlarged into a full-page diagram.
    if (box[2]-box[0]) < board.width*.3 or (box[3]-box[1]) < board.height*.3:
        return board, (0, 0, board.width, board.height)
    pad = round(min(board.size)*.025)
    box = (max(0,box[0]-pad), max(0,box[1]-pad),
           min(board.width,box[2]+pad), min(board.height,box[3]+pad))
    return board.crop(box), box


def compose_presenter(path, artwork, pose_id="good", pose_reason=""):
    """Enlarge the diagram; keep a small fixed sprite in a disjoint side area."""
    expected = ART_SHA256 if pose_id == "good" else pose_catalog()[pose_id]["sha256"]
    if hashlib.sha256(artwork).hexdigest() != expected:
        raise ValueError("配置するポーズの原画が台帳と一致しません")
    path = Path(path)
    with Image.open(path) as image:
        board = image.convert("RGBA")
    width, height = board.size
    if width < 64 or height < 36:
        raise ValueError("図解の画像サイズが小さすぎます")
    # Crop neutral margins, then fit without distorting text or relationships.
    board, source_box = trim_diagram(board)
    left, top = round(width*.015), round(height*.04)
    area_w, area_h = round(width*DIAGRAM_RIGHT)-left, height-2*top
    board = ImageOps.contain(board, (area_w, area_h), Image.Resampling.LANCZOS)
    board_x, board_y = left + (area_w-board.width)//2, (height-board.height)//2
    canvas = Image.new("RGBA", (width, height), BACKGROUND)
    canvas.alpha_composite(board, (board_x, board_y))
    with Image.open(BytesIO(artwork)) as original:
        # Ignore imperceptible alpha dust for sizing; never redraw sprite pixels.
        character = original.crop(original.getchannel("A").point(lambda v: 255 if v > 16 else 0).getbbox())
    scale = min(width * CHARACTER_WIDTH / character.width, height * CHARACTER_HEIGHT / character.height)
    character = character.resize((max(1, round(character.width * scale)),
                                  max(1, round(character.height * scale))), Image.Resampling.LANCZOS)
    x = width - round(width * 0.015) - character.width
    y = height - round(height * 0.04) - character.height
    canvas.alpha_composite(character, (x, y))
    if board_x + board.width >= x:
        raise ValueError("図解とキャラクターの領域が重なっています")
    recipe = {"version": RECIPE_VERSION, "identity_master_sha256": ART_SHA256,
              "pose_id": pose_id, "pose_reason": pose_reason[:160], "art_sha256": expected,
              "diagram_source_box": list(source_box),
              "diagram_box": [board_x, board_y, board_x + board.width, board_y + board.height],
              "character_box": [x, y, x + character.width, y + character.height]}
    meta = PngImagePlugin.PngInfo()
    meta.add_text(RECIPE_KEY, json.dumps(recipe, separators=(",", ":")))
    canvas.convert("RGB").save(path, format="PNG", pnginfo=meta)
    return recipe
