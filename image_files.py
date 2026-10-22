"""Keep interrupted image writes away from completed/reusable image paths."""
from contextlib import closing
import hashlib
from pathlib import Path
import shutil
import uuid

from PIL import Image
from image_memory import image_operation
from safe_images import open_image


@image_operation
def is_complete_image(path):
    try:
        with closing(open_image(path)) as image:
            image.verify()
        # JPEG verification alone does not decode the pixel stream.
        with closing(open_image(path)) as image:
            image.load()
        return True
    except (OSError, ValueError, SyntaxError):
        return False


def archive_incomplete_image(path, job_dir):
    """Preserve the exact incomplete bytes before a regeneration can replace it."""
    path = Path(path)
    with path.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    directory = Path(job_dir) / "images_recovery_originals"
    directory.mkdir(exist_ok=True)
    saved = directory / f"{path.stem}.{sha}{path.suffix}"
    if saved.exists():
        with saved.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != sha:
                raise OSError(f"Incomplete-image archive does not match: {saved.name}")
        return saved
    temporary = saved.with_name(f".{saved.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(path, temporary)
        with temporary.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != sha:
                raise OSError(f"Image changed while archiving: {path.name}")
        temporary.replace(saved)
    finally:
        temporary.unlink(missing_ok=True)
    return saved


def save_png_atomic(image, output_path):
    output_path = Path(output_path)
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        image.save(temporary, format="PNG")
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)
