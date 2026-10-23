"""Read-only review exports. Images travel inside the file, never through public URLs."""
from __future__ import annotations

import csv
import io
import os
import re
from pathlib import Path
from urllib.parse import quote

from PIL import ImageOps
from image_memory import image_slot

import material_store as store
from safe_images import open_image

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
HEADERS = ["番号", "画像プレビュー", "コメント", "原稿", "状態", "表示", "章", "種類",
           "画像URL", "出典", "クレジット", "要修正メモ"]
STATUS = {"ok": "生成済み・未採用", "failed": "生成失敗", "pending": "未生成",
          "generating": "生成中", "skipped": "スキップ", "skip": "スキップ",
          "thinned": "間引き", "skipped_limit": "上限でスキップ", "hold": "前の素材を継続",
          "none": "素材なし"}
DISPLAY = {"image": "素材を切り替え", "hold": "前の素材を継続", "none": "素材なし"}
ROUTES = {"diagram": "図解", "illustration": "イラスト", "realphoto": "実写風",
          "web_photo": "Web写真", "chart": "グラフ", "map": "地図", "skip": "スキップ"}


def google_client_id():
    value = os.environ.get("GOOGLE_SHEETS_CLIENT_ID", "").strip()
    return value if re.fullmatch(r"[a-zA-Z0-9-]+\.apps\.googleusercontent\.com", value) else ""


def text(value):
    # XLSX/XML cannot contain these controls. All authored cells are literal text.
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value if value is not None else ""))


def csv_text(value):
    value = text(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def review_rows(job_dir, origin="https://sozai.apprendre.jp"):
    """All scenes, with adopted assets first and generated candidates clearly labelled."""
    d = Path(job_dir)
    with store.LOCK:
        library = store.load_library(d)
        sources = store.source_rows(d)
        rows, previous, chapter = [], None, None
        for row in sources:
            no = str(row["no"])
            if chapter != row.get("chapter_index", 0):
                previous = None
            chapter = row.get("chapter_index", 0)
            mode = library["display"].get(no, row.get("display") or ("none" if row.get("route") == "skip" else "image"))
            chosen = library["selections"].get(no)
            candidates = library["candidates"].get(no, [])
            candidate = store.candidate(library, no, chosen) if chosen else (candidates[-1] if candidates else None)
            path, image_url, source, credit, checksum = None, "", row.get("web_source_url", ""), row.get("attribution", ""), ""
            status = STATUS.get(row.get("status", ""), row.get("status") or "未生成")
            if candidate:
                path = store.safe_path(d, candidate["filename"])
                checksum = candidate["sha256"]
                image_url = origin + "/materials/asset/" + quote(d.name, safe="") + "/" + quote(candidate["id"], safe="")
                source, credit = candidate.get("web_source_url", source), candidate.get("attribution", credit)
                status = "採用済み" if chosen else "候補あり・未採用"
            elif row.get("filename") or row.get("web_local_file"):
                filename = row.get("filename") or row["web_local_file"]
                path = store.safe_path(d / "images", filename)
                image_url = origin + "/results/" + quote(d.name, safe="") + "/images/" + quote(filename, safe="")
                if row.get("status") not in {"failed", "generating"}:
                    status = "生成済み・未採用"
            if mode == "hold":
                if previous:
                    path, image_url, source, credit, checksum = previous
                    status = "前の素材を継続"
                else:
                    path, image_url, checksum = None, "", ""
                    status = "継続元なし・要確認"
            elif mode == "none":
                # A skipped scene can still have candidates. Show them for review,
                # but do not turn them into adopted images or continuation sources.
                previous = None
                status = ("素材なし・候補あり" if path else STATUS.get(row.get("status"), "素材なし"))
            else:
                previous = (path, image_url, source, credit, checksum) if path else None
            note = library["flags"].get(no) or row.get("verify_reason") or row.get("error", "")
            rows.append({"values": [row["no"], "", "", row.get("sentence", ""), status,
                                     DISPLAY.get(mode, mode), row.get("chapter_title") or chapter,
                                     ROUTES.get(row.get("route"), row.get("route", "")),
                                     image_url, source, credit, note], "path": path, "sha256": checksum})
        context = store.read(d / "material_context.json", {}) or {}
        manifest = store.read(d / "manifest.json", {}) or {}
        state = store.read(d / "job.json", {}) or {}
    title = text(context.get("title") or manifest.get("title") or state.get("title_override") or d.name)
    return title, rows


def review_csv(job_dir):
    title, rows = review_rows(job_dir)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    # CSV cannot embed images; the preview cell explains the usable link column.
    writer.writerow(HEADERS)
    for row in rows:
        values = list(row["values"])
        values[1] = "画像URLから開く" if values[8] else ""
        writer.writerow([csv_text(value) for value in values])
    return title, ("\ufeff" + stream.getvalue()).encode("utf-8")


def extend_legacy_csv(job_dir, raw_csv):
    """Keep all existing columns/order, append review fields using current row state."""
    _, rows = review_rows(job_dir)
    by_no = {str(row["values"][0]): row["values"] for row in rows}
    reader = csv.reader(io.StringIO(raw_csv.lstrip("\ufeff")))
    header = next(reader, [])
    no_index = next((header.index(key) for key in ("№", "番号", "No", "no") if key in header), None)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(header + ["画像URL", "生成・採用状態", "要修正メモ", "コメント"])
    for existing in reader:
        values = by_no.get(str(existing[no_index]) if no_index is not None and len(existing) > no_index else "")
        extra = [values[8], values[4], values[11], ""] if values else ["", "", "", ""]
        writer.writerow([csv_text(value) for value in existing + extra])
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def write_workbook(job_dir, destination):
    """Build the app's upload payload, decoding only one small preview at a time."""
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as SheetImage
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    title, rows = review_rows(job_dir)
    if not rows:
        raise ValueError("書き出せる素材がまだありません")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "素材レビュー"
    sheet.freeze_panes = "D2"
    sheet.sheet_view.showGridLines = False
    sheet.append(HEADERS)
    widths = [8, 38, 38, 65, 22, 22, 24, 14, 30, 30, 24, 35]
    for column, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(column)].width = width
        cell = sheet.cell(1, column)
        cell.font = Font(name="Arial", bold=True, color="FFFFFF", size=11)
        cell.fill = PatternFill("solid", fgColor="28785C")
        cell.alignment = Alignment(vertical="center")
    sheet.row_dimensions[1].height = 30
    buffers = []
    try:
        for index, row in enumerate(rows, 2):
            sheet.row_dimensions[index].height = 126
            for column, value in enumerate(row["values"], 1):
                cell = sheet.cell(index, column, text(value))
                cell.data_type = "s"  # Manuscripts beginning with '=' are not formulas.
                cell.font = Font(name="Arial", size=11, color="202B28")
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                cell.fill = PatternFill("solid", fgColor="FFF5CD" if column == 3 else ("F4F8F6" if index % 2 == 0 else "FFFFFF"))
            path = row["path"]
            if path:
                try:
                    if row["sha256"] and store.digest(path) != row["sha256"]:
                        raise ValueError("保存された候補と画像が一致しません")
                    with image_slot(), open_image(path) as original:
                        with ImageOps.exif_transpose(original) as oriented:
                            oriented.thumbnail((256, 144))
                            buffer = io.BytesIO()
                            with oriented.convert("RGBA") as rgba:
                                from PIL import Image
                                with Image.new("RGB", rgba.size, "white") as preview:
                                    with rgba.getchannel("A") as alpha:
                                        preview.paste(rgba, mask=alpha)
                                    preview.save(buffer, format="PNG")
                    buffer.seek(0)
                    buffers.append(buffer)
                    image = SheetImage(buffer)
                    image.anchor = "B" + str(index)
                    sheet.add_image(image)
                except (OSError, SyntaxError, ValueError):
                    sheet.cell(index, 2, "画像を読み込めません。画像URLから確認してください。")
                    sheet.cell(index, 5, row["values"][4] + "・画像要確認")
        sheet.auto_filter.ref = f"A1:L{len(rows) + 1}"
        workbook.properties.title = title + "｜素材レビュー"
        workbook.properties.description = "コメント列へ記入してください。書き出した時点の全場面と画像です。"
        workbook.save(destination)
    finally:
        workbook.close()
        for buffer in buffers:
            buffer.close()
    return title, len(rows)
