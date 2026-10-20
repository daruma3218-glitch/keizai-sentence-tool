"""Process-wide image budget, shared by jobs and individual regenerations.

Acquire inside the synchronous worker: cancelling an asyncio waiter must not
release memory capacity while its HTTP/Pillow work is still running.
"""
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import gc
import os
import sys
import threading

MIB = 1024 * 1024


def memory_limit_bytes():
    for name in ("/sys/fs/cgroup/memory.max",
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            value = int(Path(name).read_text().strip())
            if 0 < value < 1 << 60:
                return value
        except (OSError, ValueError):
            pass
    # Render services should fail conservatively if the cgroup is unavailable.
    return 512 * MIB if os.environ.get("RENDER_SERVICE_ID") else None


def capacity_for(limit):
    if limit is None:
        return 32
    if limit <= 768 * MIB:
        return 1
    if limit <= 1536 * MIB:
        return 2
    if limit <= 3072 * MIB:
        return 4
    return 8


MEMORY_LIMIT = memory_limit_bytes()
IMAGE_CAPACITY = capacity_for(MEMORY_LIMIT)
_slots = threading.BoundedSemaphore(IMAGE_CAPACITY)


def effective_concurrency(requested):
    return max(1, min(int(requested), IMAGE_CAPACITY))


def release_unused_memory():
    gc.collect()
    if sys.platform.startswith("linux"):
        # Return freed Pillow/HTTP allocations to the OS between images. No-op
        # on libc implementations without malloc_trim.
        try:
            import ctypes
            ctypes.CDLL(None).malloc_trim(0)
        except (AttributeError, OSError):
            pass


@contextmanager
def image_slot():
    with _slots:
        try:
            yield
        finally:
            release_unused_memory()


def image_operation(function):
    @wraps(function)
    def bounded(*args, **kwargs):
        with image_slot():
            return function(*args, **kwargs)
    return bounded
