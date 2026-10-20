"""Exercise real image decoding/encoding and a timed-out concurrent API worker."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import generator
import chart_restyle
import image_memory


def png(size=(96, 54), mode="RGB"):
    with BytesIO() as out, Image.new(mode, size, "white") as image:
        image.save(out, "PNG")
        return out.getvalue()


def test_memory_budget_applies_to_small_jobs_and_all_generator_instances(monkeypatch):
    assert image_memory.capacity_for(512 * image_memory.MIB) == 1
    assert image_memory.capacity_for(1024 * image_memory.MIB) == 2
    monkeypatch.setattr(image_memory, "IMAGE_CAPACITY", 1)
    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: object())
    instance = generator.ParallelImageGenerator("gpt-image", openai_api_key="offline", concurrency=4)
    assert instance.concurrency == 1
    assert image_memory.effective_concurrency(12) == 1


def test_progress_shows_effective_capacity_without_rewriting_saved_preference(monkeypatch):
    import app as appmod
    state = {"status": "running", "concurrency": 4}
    monkeypatch.setattr(appmod, "_get_job_state", lambda _: state)
    monkeypatch.setattr(appmod, "APP_PASSWORD", "")
    monkeypatch.setattr(image_memory, "IMAGE_CAPACITY", 1)
    response = appmod.app.test_client().get("/api/status/job")
    assert response.status_code == 200
    assert response.json["concurrency"] == 4
    assert response.json["effective_concurrency"] == 1
    assert state == {"status": "running", "concurrency": 4}


@pytest.mark.parametrize("mode,size,fill,expected", [
    ("RGB", (96, 54), "pad", (96, 54)),
    ("RGBA", (96, 64), "crop", (96, 54)),
    ("P", (96, 64), "pad", (114, 64)),
])
def test_image_buffers_close_without_changing_resolution(tmp_path, monkeypatch, mode, size, fill, expected):
    data = png(size, mode)
    opened = []
    original_open = Image.open
    def tracked(*args, **kwargs):
        image = original_open(*args, **kwargs)
        opened.append(image)
        return image
    monkeypatch.setattr(Image, "open", tracked)
    path = tmp_path / "image.png"
    generator._save_as_16_9(data, path, fill=fill)
    for image in opened:
        with pytest.raises(ValueError):
            image.getpixel((0, 0))
    with original_open(path) as result:
        assert result.size == expected
        assert result.convert("RGB").getpixel((0, 0)) == (255, 255, 255)


def test_timed_out_api_keeps_slot_until_real_worker_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr(image_memory, "_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(image_memory, "release_unused_memory", lambda: None)
    first_entered, first_release, second_entered = threading.Event(), threading.Event(), threading.Event()
    buffers = []
    encoded = base64.b64encode(png()).decode()

    def first_api(**kwargs):
        buffers.append(kwargs["image"])
        assert kwargs["size"] == "1536x864" and kwargs["quality"] == "medium"
        first_entered.set()
        assert first_release.wait(5)
        return SimpleNamespace(data=[SimpleNamespace(b64_json=encoded, url=None)])

    def second_api(**kwargs):
        buffers.extend(kwargs["image"])
        second_entered.set()
        return SimpleNamespace(data=[SimpleNamespace(b64_json=encoded)])

    reference = tmp_path / "chart.png"
    reference.write_bytes(png())
    async def scenario():
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = loop.run_in_executor(pool, lambda: generator._sync_generate_image_openai(
                SimpleNamespace(images=SimpleNamespace(edit=first_api)), "scene", tmp_path / "one.png",
                size="1536x864", quality="medium", reference_bytes=png()))
            try:
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(first, timeout=0.1)
                assert first_entered.is_set()
                second = loop.run_in_executor(pool, lambda: chart_restyle.redraw_chart(
                    SimpleNamespace(images=SimpleNamespace(edit=second_api)),
                    reference, tmp_path / "two.png", model="gpt-image-2", quality="medium"))
                await asyncio.sleep(0.1)
                assert not second_entered.is_set(), "cancelled waiter must not release the image slot"
            finally:
                first_release.set()
            assert (await second)[0]
    asyncio.run(scenario())
    assert (tmp_path / "one.png").exists() and (tmp_path / "two.png").exists()
    assert all(stream.closed for stream in buffers)
