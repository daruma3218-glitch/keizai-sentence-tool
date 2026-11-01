"""Overlap provider generation without buffering several full image responses."""
from contextlib import ExitStack, contextmanager
import threading

from image_memory import image_slot, request_slot

_body_budget = threading.local()


def before_gemini_image_body(response):
    """HTTPX calls response hooks after headers and before reading the body."""
    acquire = getattr(_body_budget, "acquire", None)
    if acquire is None:
        raise RuntimeError("Gemini image response has no memory budget")
    acquire()


@contextmanager
def deferred_gemini_body():
    previous = getattr(_body_budget, "acquire", None)
    with ExitStack() as images:
        held = False
        def acquire():
            nonlocal held
            if not held:
                images.enter_context(image_slot())
                held = True
        _body_budget.acquire = acquire
        try:
            yield
        finally:
            _body_budget.acquire = previous


def consume_gemini_image(client, consume, *, defer_body=False, **kwargs):
    with request_slot():
        # Old/fake clients without the installed response hook stay conservative.
        with deferred_gemini_body() if defer_body else image_slot():
            return consume(client.models.generate_content(**kwargs))


def consume_openai_image(images, method, consume, **kwargs):
    """Call the existing Images API once; parse, decode and save under one budget.

    This is HTTP body streaming, not partial-image generation. Model, quality,
    dimensions and billing parameters are passed through unchanged. Non-streaming
    test/legacy clients retain the conservative complete-operation memory lock.
    """
    streaming = getattr(images, "with_streaming_response", None)
    with request_slot():
        if streaming is None:
            with image_slot():
                return consume(getattr(images, method)(**kwargs))
        with ExitStack() as responses:
            response = responses.enter_context(getattr(streaming, method)(**kwargs))
            with image_slot():
                try:
                    return consume(response.parse())
                finally:
                    # Drop the SDK's raw body and parsed-model cache before the
                    # next response may consume the same memory budget.
                    responses.close()
                    response = None
