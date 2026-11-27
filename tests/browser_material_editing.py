"""Exercise the candidate strip and Chrome's IME with local, disposable Russian-channel jobs."""
from io import BytesIO
from urllib.parse import urlsplit

from PIL import Image
from playwright.sync_api import expect

from browser_material_comments import seed, store


def exercise_editing(browser, origin, root, width):
    job = f"editing{width}"
    seed(root, job)
    directory = root / "output" / job
    store.save(directory / "material_context.json", {"source_channel_id":"roshia", "profile":{}})
    store.save(directory / "job.json", {"status":"completed", "channel_id":"roshia"})
    lib = store.load_library(directory)
    for no in (1, 2):
        for n in range(4):
            path = directory / "images" / f"sample-{no}-{n}.png"
            with Image.new("RGB", (64, 36), (90, 120 + 15*n, 150 + no)) as image:
                image.save(path)
            store.snapshot(directory, lib, no, path, f"候補{n}")
    store.commit_library(directory, lib, "fixture")
    writes, errors = [], []
    context = browser.new_context(viewport={"width":width, "height":900}, service_workers="block")
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))

    def local_only(route):
        request = route.request
        parsed = urlsplit(request.url)
        if parsed.netloc == urlsplit(origin).netloc:
            if parsed.path.endswith("/edit-note"):
                writes.append(request.post_data_json["note"])
            if (request.method == "GET" or parsed.path.endswith(("/sync", "/selection", "/edit-note"))
                    or (parsed.path.endswith("/variant") and b'name="kind"\r\n\r\nupload' in request.post_data_buffer)):
                return route.continue_()
        errors.append(parsed.path)
        route.abort()

    context.route("**/*", local_only)
    try:
        page.goto(f"{origin}/materials/jobs/{job}")
        first = page.locator('article[data-no="1"]')
        other = page.locator('article[data-no="2"] .candidate-strip')
        strip = first.locator(".candidate-strip")
        cards = first.locator(".candidate")
        expect(cards).to_have_count(6)
        strip.evaluate("s => s.scrollLeft = s.scrollWidth")
        other.evaluate("s => s.scrollLeft = 113")
        pick = cards.last
        with page.expect_response(lambda r: r.url.endswith("/selection") and r.ok):
            adopt = pick.locator('[data-action="select"]')
            if width >= 700:
                adopt.click()
            else:
                # A driver click can scroll the clipped card independently of the app.
                adopt.evaluate("b => b.click()")
        expect(pick.locator('[data-action="clear"]')).to_be_visible()
        at_end = "s => s.scrollLeft > 0 && s.scrollWidth - s.clientWidth - s.scrollLeft <= 2"
        assert strip.evaluate(at_end), "採用で右端から移動"
        assert other.evaluate("s => s.scrollLeft") == 113
        strip.evaluate("s => s.scrollLeft = 235")
        with page.expect_response(lambda r: r.url.endswith("/selection") and r.ok):
            cards.nth(2).locator('[data-action="append"]').evaluate("b => b.click()")
        expect(first.locator(".candidate.selected")).to_have_count(2)
        assert strip.evaluate("s => s.scrollLeft") == 235
        strip.evaluate("s => s.scrollLeft = s.scrollWidth")
        strip.locator('[data-action="upload"]').click()
        image_file = BytesIO()
        with Image.new("RGB", (80, 45), "#a8b9c4") as image:
            image.save(image_file, "PNG")
        page.locator("#image-file").set_input_files({"name":"追加.png", "mimeType":"image/png", "buffer":image_file.getvalue()})
        page.locator("#variant-submit").click()
        expect(cards).to_have_count(7)
        assert strip.evaluate(at_end), "取り込みで新しい右端から移動"
        assert other.evaluate("s => s.scrollLeft") == 113

        cdp = context.new_cdp_session(page)
        check_composition(page, cdp, first.locator(".candidate.selected textarea").first, writes)
        no_image = page.locator('article[data-no="3"]')
        scene_note = no_image.locator(".edit-note-input")
        check_composition(page, cdp, scene_note, writes)
        no_image.locator('[data-action="compare"]').click()
        dialog = page.locator("#preview-dialog")
        check_composition(page, cdp, dialog.locator("#review-scene-input"), writes)
        dialog.locator('[data-close="preview-dialog"]').click()
        first.locator('[data-action="compare"]').click()
        check_composition(page, cdp, dialog.locator("#review-note-input"), writes)
        assert len(store.load_library(directory)["candidates"]["1"]) == 7
        assert not errors, errors
        print(f"Chrome {width}px: strip position, upload and IME on four note fields passed", flush=True)
    finally:
        context.close()


def check_composition(page, cdp, field, writes):
    expect(field).to_be_visible()
    field.focus()
    field.evaluate("t => { window.imeField = t; t.select(); }")
    before = len(writes)
    text = f"編集指示{before + 1}"
    cdp.send("Input.imeSetComposition", {"text":"へんしゅう", "selectionStart":5, "selectionEnd":5})
    # A server response that used to replace the active textarea must leave preedit alone.
    with page.expect_response(lambda r: r.url.endswith("/sync") and r.ok):
        page.locator("#refresh").evaluate("b => b.click()")
    expect(page.locator("#refresh")).to_be_enabled()
    page.evaluate("window.dispatchEvent(new Event('pagehide'))")
    page.wait_for_timeout(2800)
    assert len(writes) == before, f"変換中の送信: {writes[before:]}"
    assert field.evaluate("t => t === window.imeField && t === document.activeElement")
    expect(field).to_have_value("へんしゅう")
    cdp.send("Input.insertText", {"text":text})
    expect(field).to_have_value(text)
    page.wait_for_timeout(1050)
    assert len(writes) == before, "入力確定直後に保存"
    if before % 2:
        field.blur()  # Leaving a committed note saves it immediately.
    expect(field.locator("..").locator(".note-state")).to_have_text("✓ 保存", timeout=6000)
    assert writes[before:] == [text], writes[before:]
