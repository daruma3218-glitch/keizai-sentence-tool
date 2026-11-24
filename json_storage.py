"""Publish complete JSON files so polling requests can always read the last saved version."""
import json
import os
import tempfile
import time
from pathlib import Path


def _retry_if_busy(operation):
    # Windows can briefly deny access to either reader or writer during replacement.
    for attempt in range(20):
        try:
            return operation()
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.025)


def read(path):
    path = Path(path)
    return json.loads(_retry_if_busy(lambda: path.read_text(encoding="utf-8")))


def save(path, value, *, indent=2):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".j",
                                         delete=False) as stream:
            temp = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=indent)
        _retry_if_busy(lambda: os.replace(temp, path))
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
