"""生成の中止の受付（2026-10-22 の見直し：素材スタジオには止める手段が無かった）。

止めたい回のフォルダを覚えておき、生成の区切り（段階の進み・画像1枚ごと）で確かめて止める。
作り終えた画像・記録は消さない。このプロセスの中だけで効く（再起動の後は「中断」になる）。
"""
from __future__ import annotations

import threading
from pathlib import Path

_LOCK = threading.Lock()
_REQUESTED: set[str] = set()


class JobCancelled(Exception):
    """利用者が止めた（失敗ではない）。"""


def _key(job_dir) -> str:
    return str(Path(job_dir).resolve()).casefold()


def request(job_dir) -> None:
    with _LOCK:
        _REQUESTED.add(_key(job_dir))


def requested(job_dir) -> bool:
    with _LOCK:
        return _key(job_dir) in _REQUESTED


def clear(job_dir) -> None:
    with _LOCK:
        _REQUESTED.discard(_key(job_dir))
