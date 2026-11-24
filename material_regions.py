"""Map the editor's closed outlines from the displayed candidate to its original pixels."""
from dataclasses import dataclass
from io import BytesIO
import json

from PIL import Image, ImageDraw, ImageOps

from image_files import save_png_atomic
from image_memory import image_operation
from safe_images import open_image

SCALE = 1000
MAX_REGIONS = 8
MAX_POINTS = 128
FRAME = (1536, 864)


class RegionEditError(ValueError):
    """An editor-facing error about the selected source or region."""


def parse_regions(raw):
    error = "修正範囲を描き直してください。1回に8か所まで囲めます。"
    if not isinstance(raw, str) or not raw or len(raw) > 18000:
        raise ValueError(error)
    try:
        regions = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError(error) from None
    if not isinstance(regions, list) or not 1 <= len(regions) <= MAX_REGIONS:
        raise ValueError(error)
    for polygon in regions:
        if not isinstance(polygon, list) or not 3 <= len(polygon) <= MAX_POINTS:
            raise ValueError(error)
        for point in polygon:
            if (not isinstance(point, list) or len(point) != 2
                    or any(type(n) is not int or not 0 <= n <= SCALE for n in point)):
                raise ValueError(error)
        twice_area = sum(x * polygon[i-1][1] - y * polygon[i-1][0]
                         for i, (x, y) in enumerate(polygon))
        if abs(twice_area) < 16:
            raise ValueError("範囲が小さすぎます。直したいものの周りを少し広めに囲んでください。")
    return regions


def _selection(size, polygons):
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    w, h = size
    for poly in polygons:
        draw.polygon([(round(x * (w - 1) / SCALE), round(y * (h - 1) / SCALE))
                      for x, y in poly], fill=255)
    return mask


def _png(image):
    with BytesIO() as buf:
        image.save(buf, format="PNG")
        return buf.getvalue()


@dataclass(frozen=True)
class RegionEdit:
    source_png: bytes
    mask_png: bytes
    content_box: tuple


@image_operation
def prepare(source_path, polygons, provider):
    """Use one coordinate system for the clean reference, provider mask and returned image."""
    with open_image(source_path) as original:
        if original.width * original.height > 20_000_000:
            raise RegionEditError("部分修正には2,000万画素以下の画像を使ってください。")
        ImageOps.exif_transpose(original, in_place=True)
        scale = min(FRAME[0] / original.width, FRAME[1] / original.height)
        size = tuple(max(1, round(side * scale)) for side in original.size)
        x, y = ((side - inner) // 2 for side, inner in zip(FRAME, size))
        box = (x, y, x + size[0], y + size[1])
        with (Image.new("RGBA", FRAME, "white") as frame,
              original.resize(size, Image.Resampling.LANCZOS) as fitted, fitted.convert("RGBA") as rgba):
            frame.paste(rgba, (x, y))
            source_png = _png(frame)
    with Image.new("L", FRAME, 0) as map_image, _selection(size, polygons) as selected:
        map_image.paste(selected, (x, y))
        if provider == "gpt-image":
            # OpenAI edits transparent pixels of a same-size PNG mask on the first input.
            with Image.new("RGBA", FRAME, "white") as mask, ImageOps.invert(map_image) as alpha:
                mask.putalpha(alpha)
                mask_png = _png(mask)
        else:
            # Gemini receives the binary location guide as the last separate reference.
            mask_png = _png(map_image)
    return RegionEdit(source_png, mask_png, box)


@image_operation
def restore_outside(source_path, result_path, region, polygons):
    """Publish only selected output pixels, at the source's oriented size and with its alpha."""
    with open_image(result_path) as result, result.resize(FRAME, Image.Resampling.LANCZOS) as frame:
        content = frame.crop(region.content_box)
    with content, open_image(source_path) as source:
        ImageOps.exif_transpose(source, in_place=True)
        mode = "RGBA" if "A" in source.getbands() or "transparency" in source.info else "RGB"
        final = source if source.mode == mode else source.convert(mode)
        with final, content.resize(source.size, Image.Resampling.LANCZOS) as fitted, _selection(source.size, polygons) as selected:
            final.paste(fitted, (0, 0), selected)
            save_png_atomic(final, result_path)
