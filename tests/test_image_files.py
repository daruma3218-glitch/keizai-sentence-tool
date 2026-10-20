from io import BytesIO
from pathlib import Path
import sys

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as appmod
from image_files import is_complete_image, save_png_atomic
from pipeline import SentencePipeline


def test_partial_large_png_is_archived_and_not_reused(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    with BytesIO() as buffer, Image.effect_noise((128, 128), 100) as image:
        image.save(buffer, "PNG")
        original = buffer.getvalue()
    partial = original[:len(original) // 2]
    assert len(partial) > 1024
    path = images / "2.png"
    path.write_bytes(partial)
    pipe = SentencePipeline("原稿" * 100, output_dir=tmp_path)
    assert pipe._existing_image(2) == ""
    archived = list((tmp_path / "images_recovery_originals").glob("2.*.png"))
    assert len(archived) == 1
    assert archived[0].read_bytes() == partial
    assert path.read_bytes() == partial
    assert appmod._find_existing_scene_image(tmp_path, 2, {"filename": "2.png"}) is None
    assert len(list((tmp_path / "images_recovery_originals").iterdir())) == 1
    path.write_bytes(original)
    assert pipe._existing_image(2) == "2.png"


def test_atomic_save_preserves_previous_image_on_interrupted_write(tmp_path):
    target = tmp_path / "2.png"
    with Image.new("RGB", (96, 54), "white") as image:
        save_png_atomic(image, target)
    original = target.read_bytes()

    class BrokenSave:
        def save(self, path, **kwargs):
            path.write_bytes(b"partial image")
            raise OSError("interrupted write")

    with pytest.raises(OSError, match="interrupted"):
        save_png_atomic(BrokenSave(), target)
    assert target.read_bytes() == original
    assert is_complete_image(target)
    assert list(tmp_path.iterdir()) == [target]


def test_failed_archive_stops_recovery_before_overwrite(tmp_path, monkeypatch):
    images = tmp_path / "images"
    images.mkdir()
    path = images / "2.png"
    partial = b"unfinished png"
    path.write_bytes(partial)

    def fail_archive(*args):
        raise OSError("archive disk unavailable")

    monkeypatch.setattr("image_files.archive_incomplete_image", fail_archive)
    pipe = SentencePipeline("原稿" * 100, output_dir=tmp_path)
    with pytest.raises(OSError, match="archive disk"):
        pipe._existing_image(2)
    with pytest.raises(OSError, match="archive disk"):
        appmod._find_existing_scene_image(tmp_path, 2, {"filename": "2.png"})
    assert path.read_bytes() == partial
