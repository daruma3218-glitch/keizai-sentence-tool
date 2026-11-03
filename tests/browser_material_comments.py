"""Run the optional real Chrome checks with synthetic data. Pass a screenshot directory."""
import csv
import io
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from openpyxl import load_workbook
from PIL import Image, ImageDraw
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import material_store as store


def seed(root, job):
    d = root / "output" / job
    (d / "images").mkdir(parents=True)
    rows = [{"no":i, "chapter_index":1, "chapter_title":"価格のしくみ", "sentence":sentence,
             "display":"image", "status":"ok", "filename":f"{i}.png"}
            for i, sentence in enumerate(["仕入れた商品が店頭に届きます。", "価格の変化を図で比べます。"], 1)]
    rows.append({"no":3, "chapter_index":1, "sentence":"前の画像を続けます。", "display":"hold", "status":"hold"})
    store.save(d / "material_context.json", {"source_channel_id":"keizai", "title":"合成データ｜画像の確認", "profile":{}})
    store.save(d / "job.json", {"status":"completed", "channel_id":"keizai"})
    store.save(d / "rows_progress.json", {"rows":rows})
    lib = store.load_library(d)
    for n, no in enumerate([1, 1, 2]):
        path = d / "images" / ("alt.png" if n == 1 else f"{no}.png")
        with Image.new("RGB", (640, 230), ["#e6f0ec","#eef2f6","#f0eadf"][n]) as image:
            draw = ImageDraw.Draw(image)
            for x,h in enumerate(([56,84,122],[55,100,148],[126,94,70])[n]):
                draw.rounded_rectangle((140+x*118,190-h,221+x*118,190),radius=8,fill=["#327361","#597a9c","#a1713c"][n])
            draw.line((90,192,544,192),fill="#546661",width=3)
            image.save(path)
        candidate = store.snapshot(d, lib, no, path, "別の候補" if n == 1 else "最初の候補")
        if n != 1:
            lib["selections"][str(no)] = candidate["id"]
    store.commit_library(d, lib, "fixture")


def exercise(browser, origin, width, output):
    job = f"sample{width}"
    attempts, writes, workbooks, errors = [], [], [], []
    page_url = f"{origin}/materials/jobs/{job}"

    def intercept(route):
        req, parsed = route.request, urlsplit(route.request.url)
        if parsed.netloc == urlsplit(origin).netloc:
            if req.method == "POST":
                writes.append(parsed.path)
            allowed = (parsed.path.endswith(("/sync","/comments","/review.xlsx","/display-name")) or "/comments/c_" in parsed.path)
            if req.method != "GET" and not allowed:
                attempts.append(parsed.path)
                return route.abort()
            return route.continue_()
        if parsed.hostname == "www.googleapis.com":
            cors = {"Access-Control-Allow-Origin":origin, "Access-Control-Expose-Headers":"Location",
                    "Access-Control-Allow-Methods":"GET, POST, PUT, OPTIONS",
                    "Access-Control-Allow-Headers":"*"}
            if req.method == "OPTIONS":
                return route.fulfill(status=204, headers=cors)
            if req.method == "POST":
                return route.fulfill(status=200, headers={**cors,"Location":"https://www.googleapis.com/upload/drive/v3/files?upload_id=fixture"})
            if req.method == "PUT":
                workbooks.append(req.post_data_buffer)
            return route.fulfill(headers=cors, json={"id":"fixture", "mimeType":"application/vnd.google-apps.spreadsheet",
                                       "webViewLink":"https://docs.google.com/spreadsheets/d/fixture/edit"})
        attempts.append(parsed.hostname)
        route.abort()

    def open_page(who):
        ctx = browser.new_context(viewport={"width":width, "height":844 if width == 390 else 900},
                                  extra_http_headers={"X-Synthetic-User":who}, service_workers="block")
        ctx.route("**/*", intercept)
        ctx.add_init_script("""window.google = {accounts:{oauth2:{
            hasGrantedAllScopes:() => true, initTokenClient:({callback}) => ({requestAccessToken:() => callback({access_token:'fixture'})})
        }}};""")
        page = ctx.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(page_url)
        page.locator('[data-no="1"] [data-action="comments"]').first.click()
        return ctx, page

    ctx_a, a = open_page("review"+str(width))
    ctx_b, b = open_page("artist"+str(width))
    try:
        expect(a.locator("#review-title")).to_have_text("画像のコメント")
        expect(a.locator("#review-adopt-next")).not_to_be_visible()
        expect(a.locator("#review-reference")).not_to_be_visible()
        expect(a.locator("#comment-position")).to_have_text("1 / 2 場面")
        expect(a.locator("#comment-prev-scene")).to_be_disabled()
        assert a.locator("#review-reference-image").get_attribute("src") is None
        a.screenshot(path=output / f"empty-{width}.png")
        a.locator("#comment-name").fill("確認係")
        a.locator("#comment-body").fill("図の左に単位を追加してください。")
        a.locator("#comment-body").press("Enter")
        expect(a.locator(".comment-thread")).to_have_count(0)
        a.locator("#comment-submit").click()
        expect(a.locator(".comment-thread")).to_have_count(1)
        a.locator("#comment-next-scene").click()
        expect(a.locator("#review-position")).to_have_text("2 / 2 場面")
        expect(a.locator(".comment-thread")).to_have_count(0)
        a.locator("#comment-body").fill("合成の場面2を確認しました。")
        a.locator("#comment-submit").click()
        expect(a.locator(".comment-thread")).to_have_count(1)
        expect(a.locator("#comment-next-scene")).to_be_disabled()
        a.locator("#comment-prev-scene").click()
        expect(a.locator(".comment-thread")).to_have_count(1)
        # Read the same candidate in the other user's tab; leave tab A to its normal 30-second timer.
        b.reload()
        b.locator('[data-no="1"] [data-action="comments"]').first.click()
        b.locator("#comment-name").fill("作り手")
        b.get_by_role("button", name="返信", exact=True).click()
        b.locator("#comment-body").fill("単位を円にそろえます。")
        a.locator("#comment-body").fill("書きかけのメモ")
        started = time.monotonic()
        b.locator("#comment-submit").click()
        expect(a.locator(".comment-reply")).to_contain_text("単位を円にそろえます。", timeout=30000)
        elapsed = time.monotonic() - started
        assert elapsed <= 30, elapsed
        expect(a.locator("#comment-body")).to_have_value("書きかけのメモ")
        a.locator("#comment-body").fill("")
        a.locator("#comment-heading").scroll_into_view_if_needed()
        a.screenshot(path=output / f"comments-{width}.png")
        a.locator("#review-body").evaluate("el => el.scrollTop = 0")
        a.screenshot(path=output / f"scene-{width}.png")
        assert a.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert a.locator("#preview-dialog").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        image_box = a.locator("#preview-image").bounding_box()
        discussion_box = a.locator("#image-comments").bounding_box()
        if width == 390:
            assert discussion_box["y"] >= image_box["y"] + image_box["height"]
        else:
            assert discussion_box["x"] >= image_box["x"] + image_box["width"]
        footer = a.locator(".comment-navigation").bounding_box()
        a.locator("#review-body").evaluate("el => el.scrollTop = el.scrollHeight")
        assert a.locator(".comment-navigation").bounding_box() == footer
        assert all(a.locator(s).bounding_box()["height"] >= 44 for s in ("#comment-prev-scene","#comment-next-scene","#comment-submit"))
        a.locator(".comment-menu summary").first.click()
        a.get_by_role("button", name="直す", exact=True).click()
        a.locator("#comment-body").fill("図の左に円の単位を追加してください。")
        a.locator("#comment-submit").click()
        expect(a.locator(".comment-thread")).to_contain_text("円の単位")
        a.get_by_role("button", name="対応済みにする").click()
        expect(a.locator(".comment-thread")).to_have_count(0)
        a.locator("#comment-show-resolved").check()
        expect(a.locator(".comment-thread.resolved")).to_have_count(1)
        a.get_by_role("button", name="もう一度開く").click()
        expect(a.locator(".comment-thread:not(.resolved)")).to_have_count(1)
        a.locator("#review-candidate-id").select_option(index=1)
        a.get_by_role("button", name="前の候補へのコメント（候補1）").click()
        expect(a.locator(".comment-thread")).to_contain_text("円の単位")
        a.locator("#comment-next-scene").click()
        a.locator(".comment-menu summary").first.click()
        a.once("dialog", lambda dialog: dialog.accept())
        a.get_by_role("button", name="取り消す", exact=True).click()
        expect(a.locator(".comment-thread")).to_contain_text("取り消されたコメント")
        a.locator('[data-close="preview-dialog"]').click()
        a.locator('[data-filter="comments"]').click()
        expect(a.locator(".scene")).to_have_count(1)
        a.locator("#comment-review-start").click()
        expect(a.locator("#comment-position")).to_have_text("1 / 1 場面")
        expect(a.locator("#comment-next-scene")).to_be_disabled()
        a.locator('[data-close="preview-dialog"]').click()
        a.locator('[data-action="compare"]').first.click()
        expect(a.locator("#review-adopt-next")).to_be_visible()
        expect(a.locator("#review-reference")).to_be_visible()
        expect(a.locator(".comment-navigation")).not_to_be_visible()
        expect(a.locator("#review-position")).to_have_text("1 / 3 場面")
        a.locator('[data-close="preview-dialog"]').click()
        a.locator('[data-action="edit"]').first.click()
        a.get_by_role("button", name="指示に入れる").first.click()
        expect(a.locator("#instruction")).to_have_value("図の左に円の単位を追加してください。")
        expect(a.locator("#variant-comment-status")).to_have_text("指示欄の末尾に追加しました。")
        a.screenshot(path=output / f"instruction-{width}.png")
        a.locator('#variant-dialog button[data-close]').first.click()
        a.locator("#download").click()
        expect(a.locator("#export-warning")).to_contain_text("未対応のコメントが 1 件あります")
        expect(a.locator("#export-confirm")).to_be_enabled()
        a.locator('#export-dialog button[data-close]').first.click()
        a.locator(".review-export-details > summary").click()
        expect(a.locator("[data-sheet-comments]")).to_have_value("all")
        a.locator("[data-sheet-comments]").select_option("open")
        with a.expect_download() as event:
            a.locator("[data-review-csv]").click()
        with open(event.value.path(), encoding="utf-8-sig", newline="") as f:
            csv_rows = list(csv.reader(f))
        assert csv_rows[0][2:4] == ["コメント", "スタジオのコメント"] and csv_rows[1][2] == ""
        assert "円の単位" in csv_rows[1][3] and not csv_rows[2][3]
        a.locator("[data-sheet-create]").click()
        expect(a.locator("[data-sheet-status]")).to_contain_text("作成しました。")
        expect(a.locator("[data-sheet-open]")).to_be_visible()
        workbook = load_workbook(io.BytesIO(workbooks.pop()))
        assert workbook.active["C1"].value == "コメント" and workbook.active["D1"].value == "スタジオのコメント"
        assert workbook.active["D2"].value == csv_rows[1][3]
        workbook.close()
        a.screenshot(path=output / f"export-{width}.png")
        assert not any(path.endswith("/selection") for path in writes)
        before = list(writes)
        a.goto(page_url + "?view=saved")
        a.locator('[data-no="1"] [data-action="comments"]').first.click()
        expect(a.locator(".comment-thread")).to_contain_text("円の単位")
        assert before == writes
        if width == 390:
            # A long unbroken line and the maximum-length display name must also fit small screens.
            a.locator("#comment-rename").click()
            a.locator("#comment-name").fill("確"*40)
            a.locator("#comment-name-save").click()
            expect(a.locator("#comment-author")).to_have_text("確"*40)
            a.locator("#comment-jump").click()
            expect(a.locator("#comment-body")).to_be_focused()
            a.locator("#comment-body").fill("A"*1000)
            a.locator("#comment-submit").click()
            expect(a.locator(".comment-thread")).to_have_count(2)
            for w,h in [(320,568),(768,600)]:
                a.set_viewport_size({"width":w, "height":h})
                a.locator("#comment-jump").click()
                expect(a.locator("#comment-body")).to_be_focused()
                assert a.locator("#review-body").evaluate("el => el.scrollWidth <= el.clientWidth+1")
                nav = a.locator(".comment-navigation").bounding_box()
                assert nav["y"] >= 0 and nav["y"]+nav["height"] <= h+1
        assert not attempts and not errors, (attempts, errors)
        print(f"Chrome {width}px: editing, two users, polling {elapsed:.1f}s, filter, instruction, ZIP warning, Google stub and CSV passed", flush=True)
    finally:
        ctx_b.close()
        ctx_a.close()


def main(output):
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="studio-comment-browser-") as tmp:
        root = Path(tmp)
        with patch.dict(os.environ, {"DATA_DIR":tmp, "APP_PASSWORD":"", "CF_ACCESS_AUD":"",
                                     "RENDER_SERVICE_ID":"", "SECRET_KEY":"isolated-browser-fixture",
                                     "GOOGLE_SHEETS_CLIENT_ID":"fixture.apps.googleusercontent.com"}):
            import app
            import cloudflare_access
            for width in [390, 1366]:
                seed(root, f"sample{width}")
            # Local-only synthetic actors. Signed JWTs and invalid proofs are covered by the API tests.
            with patch.object(cloudflare_access, "verified_email", lambda h:h.get("X-Synthetic-User", "")), \
                 patch.object(app, "APP_PASSWORD", ""), patch.object(app, "resolve_channel_keys", return_value={k:"" for k in ("openai","gemini","anthropic")}):
                logging.getLogger("werkzeug").setLevel(logging.ERROR)
                server = make_server("127.0.0.1", 0, app.app, threaded=True)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(channel="chrome", headless=True, args=["--disable-background-networking"])
                        try:
                            for width in [390, 1366]:
                                exercise(browser, f"http://127.0.0.1:{server.server_port}", width, output)
                        finally:
                            browser.close()
                finally:
                    server.shutdown()
                    worker.join()
                    server.server_close()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
