"""キャラ固定の見本を複数枚にする（2026-11-15 社長「ドライブに上がっているイラストを参考として取り入れて、
今後はずれイラストがでないようにすること」）。

チャンネル設定の character_ref（基準画像1枚）はそのまま使い、同じ名前の「_set」フォルダに catalog.json が
あれば、その画像を基準画像の後ろに足す。例: references/keizai_professor.png → references/keizai_professor_set/。

- 画像のSHA-256を catalog.json と照らし、違う・欠けている画像は使わずに理由をログに出す（基準画像だけで続ける）。
- 見本を増やすと入力の費用が増えるので、先生が描かれる画像と手直しにだけ全部を渡す（generator 側で選ぶ）。
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

CATALOG = "catalog.json"
MIN_BYTES = 200


def _mime(path: Path) -> str:
    return "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"


def set_dir_for(primary_path) -> Path:
    p = Path(primary_path)
    return p.with_name(p.stem + "_set")


def load_reference_set(primary_path) -> list[tuple[bytes, str]]:
    """基準画像の後ろに足す見本を (bytes, mime) の並びで返す。フォルダ・catalog が無ければ空。"""
    if not primary_path:
        return []
    folder = set_dir_for(primary_path)
    catalog_path = folder / CATALOG
    if not catalog_path.exists():
        return []
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("[character-refs] %s を読めないため、基準画像1枚で続けます (%s)", catalog_path, exc)
        return []
    out = []
    for entry in catalog.get("images") or []:
        name = str((entry or {}).get("file") or "")
        expected = str((entry or {}).get("sha256") or "").lower()
        path = folder / name
        if not name or Path(name).name != name or not path.is_file():
            log.warning("[character-refs] 見本 %s が無いため外します", name or "(名前なし)")
            continue
        data = path.read_bytes()
        if len(data) < MIN_BYTES or hashlib.sha256(data).hexdigest() != expected:
            log.warning("[character-refs] 見本 %s が catalog.json の SHA-256 と違うため外します", name)
            continue
        out.append((data, _mime(path)))
    if out:
        log.info("[character-refs] %s の見本 %d 枚を使います", folder.name, len(out))
    return out
