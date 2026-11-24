"""Exercise real Chrome pointer, touch and keyboard input on an isolated candidate library."""
import logging
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch
from urllib.parse import urlsplit

from PIL import Image, ImageDraw
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import material_store as store


def draw_reference(path):
    with Image.new("RGB", (720, 360), "#f6f3ea") as diagram:
        draw = ImageDraw.Draw(diagram)
        for x in (200, 423):
            draw.polygon([(x-22,196),(x+2,196),(x+2,184),(x+27,210),
                          (x+2,236),(x+2,224),(x-22,224)], fill="#eead2d")
        diagram.save(path)


def exercise(browser, origin, job, candidate, width, output):
    mobile = width < 600
    context = browser.new_context(viewport={"width":width, "height":844 if mobile else 900},
                                  has_touch=mobile, is_mobile=mobile, service_workers="block")
    external, errors = [], []
    def only_local(route):
        if urlsplit(route.request.url).netloc == urlsplit(origin).netloc:
            return route.continue_()
        external.append(route.request.url)
        route.abort()
    context.route("**/*", only_local)
    page = context.new_page()
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        page.goto(origin + "/materials/jobs/" + job.name + "?view=saved")
        scene = page.locator('.scene[data-no="1"]')
        edit = scene.locator('[data-candidate="' + candidate["id"] + '"] [data-action="edit"]')
        edit.click()
        page.get_by_text("囲んだ部分", exact=True).click()
        canvas = page.locator("#region-overlay")
        expect(canvas).to_be_visible()
        expect(page.locator("#variant-submit")).to_be_disabled()
        assert page.locator("#variant-dialog").evaluate("d => d.scrollWidth <= d.clientWidth + 1")
        box = canvas.bounding_box()
        image_box = page.locator("#variant-preview").bounding_box()
        assert all(abs(box[k] - image_box[k]) < 1 for k in box)
        def select_region(cx):
            if mobile:
                page.locator("#region-tool").select_option("rect")
                canvas.tap(position={"x":box["width"]*(cx-.047), "y":box["height"]*.51})
                canvas.tap(position={"x":box["width"]*(cx+.047), "y":box["height"]*.71})
            else:
                ring = [(cx-.048,.58),(cx-.037,.51),(cx+.035,.51),(cx+.048,.60),
                        (cx+.036,.70),(cx-.04,.70),(cx-.048,.58)]
                for i,(x,y) in enumerate(ring):
                    page.mouse.move(box["x"] + box["width"]*x, box["y"] + box["height"]*y, steps=3)
                    if not i:
                        page.mouse.down()
                page.mouse.up()
        select_region(.277)
        expect(page.locator("#region-count")).to_have_text("1か所を選択")
        select_region(.588)
        page.locator("#region-undo").click()
        expect(page.locator("#region-count")).to_have_text("1か所を選択")
        select_region(.588)
        expect(canvas.locator("polygon")).to_have_count(2)
        outlines = canvas.locator("polygon").evaluate_all(
            "polys => polys.map(poly => Array.from(poly.points, point => [point.x, point.y]))")
        (output / f"region-points-{width}.json").write_text(json.dumps(outlines), encoding="utf-8")
        page.locator("#region-delete").click()
        expect(page.locator("#instruction")).to_have_value("囲んだ部分を消し、周囲に合わせて背景を補ってください。")
        # Keep the image visible after focusing the text entry on a small viewport.
        page.locator("#variant-dialog").evaluate("d => {d.scrollTop=0}")
        assert page.locator("#variant-submit").evaluate("""b => {
            const r = b.getBoundingClientRect(), d = b.closest('dialog').getBoundingClientRect();
            return r.bottom <= d.bottom && r.top >= d.top;
        }""")
        page.screenshot(path=str(output / f"region-editor-{width}.png"))
        page.locator("#variant-submit").click()
        expect(scene.get_by_text("部分修正案", exact=True)).to_be_visible(timeout=16000)
        library = store.load_library(job)
        new = next(c for c in library["candidates"]["1"] if c["label"] == "部分修正案")
        assert new["parent_id"] == candidate["id"] and library["selections"]["1"] == candidate["id"]
        with Image.open(job/candidate["filename"]) as src, Image.open(job/new["filename"]) as result:
            for x,y,changed in [(.277,.60,True),(.588,.60,True),(.44,.60,False),(.94,.50,False)]:
                pt = round(x*(src.width-1)), round(y*(src.height-1))
                assert (src.getpixel(pt) != result.getpixel(pt)) == changed
        # Check initial/reset state, keyboard selection and candidate isolation.
        scene.locator('[data-candidate="' + new["id"] + '"] [data-action="edit"]').click()
        expect(page.get_by_role("radio", name="画像全体")).to_be_checked()
        expect(page.locator("#instruction")).to_have_value("")
        page.get_by_text("囲んだ部分", exact=True).click()
        expect(page.locator("#region-count")).to_have_text("0か所を選択")
        canvas.focus()
        for key in ["Enter", "Shift+ArrowRight", "Shift+ArrowDown", "Enter"]:
            canvas.press(key)
        expect(canvas.locator("polygon")).to_have_count(1)
        page.locator("#region-undo").click()
        expect(page.locator("#variant-submit")).to_be_disabled()
        page.get_by_text("画像全体", exact=True).click()
        page.locator("#edit-person").select_option("professor")
        page.locator('#variant-dialog [data-close]').first.click()
        scene.locator('[data-candidate="' + candidate["id"] + '"] [data-action="preview"]').click()
        page.get_by_role("button", name="囲んで直す", exact=True).click()
        expect(page.locator("#region-overlay")).to_be_visible()
        expect(page.locator("#variant-submit")).to_be_disabled()
        page.keyboard.press("Escape")
        expect(page.locator("#preview-dialog")).to_be_visible()
        page.keyboard.press("Escape")
        edit.click()
        expect(page.locator("#edit-person")).to_have_value("auto")
        expect(page.locator("#region-overlay")).to_be_hidden()
        assert not errors and not external, (errors, external)
        print(f"Chrome {width}px: two selections, undo, edit, outside pixels, adoption, keyboard and reset passed.", flush=True)
    finally:
        context.close()


def main(output, reference=None):
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="region-ui-", dir=output) as tmp:
        root = Path(tmp)
        with patch.dict(os.environ, {"DATA_DIR":tmp, "APP_PASSWORD":"", "SECRET_KEY":"region-ui-test",
                                     "CF_ACCESS_AUD":"", "RENDER_SERVICE_ID":""}):
            import app
            from test_materials import make_job, choose
            def simulate_generation(**kwargs):
                assert kwargs["region_edit"] and kwargs["prompts"][0]["edit_instruction"].startswith("囲んだ部分を消し")
                with Image.new("RGB", (1536, 864), "#196d51") as altered:
                    altered.save(kwargs["output_dir"] / "1.png")
                return [{"success":True, "filename":"1.png"}]
            with (patch.object(app, "APP_PASSWORD", ""), patch.object(app, "resolve_channel_keys",
                  return_value={"openai":"offline", "gemini":"", "anthropic":""}),
                  patch("generator.run_parallel_generation", simulate_generation)):
                (root / "output").mkdir(exist_ok=True)
                samples = []
                for width in (1366, 390):
                    job, _, _ = make_job(root, "region" + str(width))
                    diagram = job / "images" / "arrows.png"
                    if reference:
                        diagram.write_bytes(reference.read_bytes())
                    else:
                        draw_reference(diagram)
                    lib = store.load_library(job)
                    c = store.snapshot(job, lib, 1, diagram, "矢印の比較")
                    store.commit_library(job, lib, "fixture")
                    choose(job, c)
                    samples.append((job, c, width))
                logging.getLogger("werkzeug").setLevel(logging.ERROR)
                server = make_server("127.0.0.1", 0, app.app, threaded=True)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    with sync_playwright() as pw:
                        browser = pw.chromium.launch(channel="chrome", headless=True, args=["--disable-background-networking"])
                        try:
                            for job,c,width in samples:
                                exercise(browser, f"http://127.0.0.1:{server.server_port}", job, c, width, output)
                        finally:
                            browser.close()
                finally:
                    server.shutdown()
                    worker.join()
                    server.server_close()


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None)
