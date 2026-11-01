"""Real pinned SDK + offline HTTP streams exercise concurrency and buffer lifetime."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from pathlib import Path
import sys
import threading
import weakref

import httpx
import openai
from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import generator
import image_memory


def image_json():
    with BytesIO() as out, Image.new("RGB", (96, 54), "white") as image:
        image.save(out, "PNG")
        return json.dumps({"data": [{"b64_json": base64.b64encode(out.getvalue()).decode()}]}).encode()


@pytest.fixture
def budgets(monkeypatch):
    monkeypatch.setattr(image_memory, "_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(image_memory, "_request_slots", threading.BoundedSemaphore(4))
    monkeypatch.setattr(image_memory, "_request_activity", {"active": 0, "peak": 0})
    monkeypatch.setattr(image_memory, "release_unused_memory", lambda: None)


def client_for(handler):
    return openai.OpenAI(api_key="offline", max_retries=0,
                         http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_four_generations_overlap_but_response_bodies_are_read_one_at_a_time(tmp_path, budgets, monkeypatch):
    all_started, first_read, release_read = threading.Event(), threading.Event(), threading.Event()
    lock = threading.Lock()
    state = {"requests": 0, "readers": 0, "peak_readers": 0, "closed": 0}
    body = image_json()
    from openai._response import APIResponse
    parsed_models = []
    original_parse = APIResponse.parse
    def checked_parse(response, *args, **kwargs):
        assert all(model() is None for model in parsed_models), "previous SDK cache must be released before parsing another image"
        parsed = original_parse(response, *args, **kwargs)
        parsed_models.append(weakref.ref(parsed))
        return parsed
    monkeypatch.setattr(APIResponse, "parse", checked_parse)

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            with lock:
                state["readers"] += 1
                state["peak_readers"] = max(state["peak_readers"], state["readers"])
            first_read.set()
            try:
                assert release_read.wait(5)
                yield body
            finally:
                with lock:
                    state["readers"] -= 1

        def close(self):
            with lock:
                state["closed"] += 1

    def handler(request):
        payload = json.loads(request.content)
        assert payload["model"] == "gpt-image-2"
        assert payload["size"] == "1536x864" and payload["quality"] == "medium" and payload["n"] == 1
        with lock:
            state["requests"] += 1
            if state["requests"] == 4:
                all_started.set()
        assert all_started.wait(5), "HTTP generation must overlap before reading image bodies"
        return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=Body())

    with client_for(handler) as client, ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(generator._sync_generate_image_openai, client, "scene", tmp_path / f"{i}.png",
                               size="1536x864", quality="medium") for i in range(5)]
        try:
            assert all_started.wait(5) and first_read.wait(5)
            with lock:
                assert state["requests"] == 4
                assert state["readers"] == 1
            assert image_memory.generation_status()["active_requests"] == 4
        finally:
            release_read.set()
        assert all(f.result(timeout=10)[0] for f in futures)
    assert state == {"requests": 5, "readers": 0, "peak_readers": 1, "closed": 5}
    assert image_memory.generation_status()["active_requests"] == 0
    assert image_memory.generation_status()["peak_active_requests"] == 4
    assert all(model() is None for model in parsed_models)
    for path in tmp_path.glob("*.png"):
        with Image.open(path) as image:
            assert image.size == (96, 54) and image.getpixel((0, 0)) == (255, 255, 255)


def test_timeout_does_not_release_remote_slot_or_block_local_image_work(tmp_path, budgets, monkeypatch):
    monkeypatch.setattr(image_memory, "_request_slots", threading.BoundedSemaphore(1))
    entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()
    count = []

    def handler(request):
        count.append(request)
        if len(count) == 1:
            entered.set()
            assert release.wait(5)
        else:
            second_entered.set()
        return httpx.Response(200, content=image_json(), headers={"Content-Type": "application/json"})

    async def scenario(client):
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = loop.run_in_executor(pool, generator._sync_generate_image_openai,
                                         client, "first", tmp_path / "first.png")
            try:
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(first, 0.1)
                assert entered.is_set()
                # Waiting at the provider no longer monopolizes the Pillow budget.
                assert image_memory._slots.acquire(timeout=0.1)
                image_memory._slots.release()
                second = loop.run_in_executor(pool, generator._sync_generate_image_openai,
                                              client, "second", tmp_path / "second.png")
                await asyncio.sleep(0.1)
                assert not second_entered.is_set()
            finally:
                release.set()
            assert (await second)[0]

    with client_for(handler) as client:
        asyncio.run(scenario(client))
    assert len(count) == 2
    assert (tmp_path / "first.png").exists() and (tmp_path / "second.png").exists()
    assert image_memory.generation_status()["active_requests"] == 0


def test_stream_is_closed_and_both_budgets_released_on_bad_image(tmp_path, budgets, monkeypatch):
    monkeypatch.setattr(generator.time, "sleep", lambda _: None)
    closed = []

    class BrokenImage(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"data":[{"b64_json":"bm90LWEtcG5n"}]}'
        def close(self):
            closed.append(True)

    with client_for(lambda _: httpx.Response(200, stream=BrokenImage(),
                    headers={"Content-Type": "application/json"})) as client:
        assert not generator._sync_generate_image_openai(client, "scene", tmp_path / "bad.png")[0]
    assert closed == [True]
    assert image_memory.generation_status()["active_requests"] == 0
    assert image_memory._slots.acquire(timeout=0.1)
    image_memory._slots.release()
    assert not (tmp_path / "bad.png").exists()


def test_size_fallback_releases_first_request_before_retry(tmp_path, budgets, monkeypatch):
    monkeypatch.setattr(image_memory, "_request_slots", threading.BoundedSemaphore(1))
    sizes = []
    def handler(request):
        size = json.loads(request.content)["size"]
        sizes.append(size)
        if size == "1536x864":
            return httpx.Response(400, json={"error": {"message": "unsupported size", "type": "invalid_request_error"}})
        return httpx.Response(200, content=image_json(), headers={"Content-Type": "application/json"})
    with client_for(handler) as client:
        assert generator._sync_generate_image_openai(client, "scene", tmp_path / "fallback.png", size="1536x864")[0]
    assert sizes == ["1536x864", "1536x1024"]
    assert image_memory.generation_status()["active_requests"] == 0


def test_small_server_request_limit_cannot_exceed_tested_ceiling(monkeypatch):
    monkeypatch.setattr(image_memory, "MEMORY_LIMIT", 512 * image_memory.MIB)
    for configured, expected in [("99", 4), ("2", 2), ("0", 1), ("invalid", 4)]:
        monkeypatch.setenv("IMAGE_REQUESTS_MAX", configured)
        assert image_memory.request_capacity() == expected


def test_streaming_edit_keeps_source_and_chart_references(tmp_path, budgets):
    import chart_restyle
    source = tmp_path / "source.png"
    with Image.new("RGB", (96, 54), "white") as image:
        image.save(source)
    original = source.read_bytes()
    requests = []
    def handler(request):
        assert request.url.path.endswith("/images/edits")
        assert "multipart/form-data" in request.headers["content-type"]
        assert b'gpt-image-2' in request.content and b'medium' in request.content
        requests.append(request.content)
        return httpx.Response(200, content=image_json(), headers={"Content-Type": "application/json"})
    with client_for(handler) as client:
        assert generator._sync_generate_image_openai(client, "edit", tmp_path / "edited.png",
                                                    reference_bytes=original, size="1536x864")[0]
        assert chart_restyle.redraw_chart(client, source, tmp_path / "chart.png",
                                         model="gpt-image-2", quality="medium", reference_path=str(source))[0]
    assert len(requests) == 2 and b'reference.png' in requests[0]
    assert b'chart.png' in requests[1] and b'professor.png' in requests[1]
    assert source.read_bytes() == original


@pytest.mark.parametrize("mixed", [False, True])
def test_gemini_and_openai_share_four_requests_and_one_body_budget(tmp_path, budgets, monkeypatch, mixed):
    from google import genai
    barrier = threading.Barrier(4)
    lock = threading.Lock()
    state = {"readers": 0, "peak": 0, "requests": 0, "closed": 0}
    openai_json = image_json()
    encoded = json.loads(openai_json)["data"][0]["b64_json"]
    gemini_json = json.dumps({"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "image/png", "data": encoded}}]}}]}).encode()

    class Body(httpx.SyncByteStream):
        def __init__(self, content):
            self.content = content
        def __iter__(self):
            with lock:
                state["readers"] += 1
                state["peak"] = max(state["peak"], state["readers"])
            try:
                yield self.content
            finally:
                with lock:
                    state["readers"] -= 1
        def close(self):
            with lock:
                state["closed"] += 1

    def handler(request):
        with lock:
            state["requests"] += 1
        is_gemini = "generateContent" in request.url.path
        if is_gemini:
            assert generator.DEFAULT_GEMINI_MODEL in request.url.path
            body = json.loads(request.content)
            assert body["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]
        barrier.wait(5)
        return httpx.Response(200, headers={"Content-Type": "application/json"},
                              stream=Body(gemini_json if is_gemini else openai_json))

    original_client = genai.Client
    def offline_client(**kwargs):
        # Exercise the real factory configuration, including its response hook.
        kwargs["http_options"].client_args["transport"] = httpx.MockTransport(handler)
        return original_client(**kwargs)
    monkeypatch.setattr(genai, "Client", offline_client)
    instance = generator.ParallelImageGenerator("nanobanana", gemini_api_key="offline", concurrency=4)
    assert instance.gemini_deferred_body and instance.concurrency == 4
    with instance.gemini_client, client_for(handler) as openai_client, ThreadPoolExecutor(max_workers=4) as pool:
        futures = []
        for i in range(8):
            path = tmp_path / f"{i}.png"
            if mixed and i % 2:
                futures.append(pool.submit(generator._sync_generate_image_openai, openai_client, "scene", path))
            else:
                futures.append(pool.submit(instance._dispatch_sync_generate, "scene", path))
        assert all(f.result(timeout=10)[0] for f in futures)
    assert state == {"readers": 0, "peak": 1, "requests": 8, "closed": 8}
    assert image_memory.generation_status()["active_requests"] == 0
    assert image_memory.generation_status()["peak_active_requests"] == 4
    assert len(list(tmp_path.glob("*.png"))) == 8


def test_gemini_failure_releases_response_hook_budget(tmp_path, budgets, monkeypatch):
    from google import genai
    from google.genai import types
    from image_transport import before_gemini_image_body
    monkeypatch.setattr(generator.time, "sleep", lambda _: None)
    with genai.Client(api_key="offline", http_options=types.HttpOptions(client_args={
        "transport": httpx.MockTransport(lambda _: httpx.Response(200, json={"candidates": [
            {"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": "bm90LWEtcG5n"}}]}}]})),
        "event_hooks": {"response": [before_gemini_image_body]}})) as client:
        assert not generator._sync_generate_image_gemini(client, "scene", tmp_path / "bad.png", defer_body=True)[0]
    assert image_memory._slots.acquire(timeout=0.1)
    image_memory._slots.release()
    assert image_memory.generation_status()["active_requests"] == 0
