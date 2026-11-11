"""Optional real Chrome checks with synthetic images and no external requests."""
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_material_comments import seed
import material_store as store


def exercise(browser, origin, root, width, output):
    job = f"visibility{width}"
    d = root / "output" / job
    errors, refused = [], []
    context = browser.new_context(viewport={"width": width, "height": 844 if width < 700 else 900},
                                  service_workers="block")
    def restrict(route):
        request = route.request
        path = urlsplit(request.url)
        allowed = path.netloc == urlsplit(origin).netloc and (
            request.method == "GET" or path.path.endswith(("/sync", "/selection")))
        if allowed:
            route.continue_()
        else:
            refused.append(path.path)
            route.abort()
    context.route("**/*", restrict)
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"{origin}/materials/jobs/{job}")
        scene = page.locator('[data-no="1"]')
        expect(scene.locator(".candidate")).to_have_count(2)
        expect(page.locator("#hidden-candidates-control")).to_be_hidden()
        original, alternate = store.load_library(d)["candidates"]["1"]
        original_files = {p: p.read_bytes() for p in d.rglob("*.png")}
        adopted = scene.locator(f'[data-candidate="{original["id"]}"]')
        other = scene.locator(f'[data-candidate="{alternate["id"]}"]')
        adopted.locator("summary").click()
        expect(adopted.locator('[data-action="hide"]')).to_be_disabled()
        adopted.locator("summary").click()
        other.locator("summary").click()
        # A click helper can scroll a clipped menu into view. Check its geometry
        # before clicking, so users do not have to discover an inner scroll area.
        assert other.evaluate("el => { const menu = el.querySelector('.candidate-more-actions').getBoundingClientRect(); const card = el.getBoundingClientRect(); return menu.bottom <= card.bottom && menu.right <= card.right; }")
        other.locator('[data-action="hide"]').click()
        expect(scene.locator(".candidate")).to_have_count(1)
        expect(page.locator("#hidden-candidates-count")).to_have_text("（1枚）")
        page.reload()
        expect(scene.locator(".candidate")).to_have_count(1)
        expect(page.locator("#show-hidden-candidates")).not_to_be_checked()
        # Hidden candidates stay out of both comparison selectors and image comments.
        scene.locator('[data-action="compare"]').click()
        expect(page.locator("#review-candidate-id option")).to_have_count(1)
        expect(page.locator("#review-visibility")).to_be_disabled()
        page.locator('[data-close="preview-dialog"]').click()
        page.locator("#show-hidden-candidates").check()
        expect(other.locator(".hidden-candidate-label")).to_have_text("非表示")
        page.screenshot(path=str(output / f"hidden-{width}.png"), full_page=True)
        other.locator('[data-action="restore"]').click()
        expect(page.locator("#hidden-candidates-control")).to_be_hidden()
        expect(scene.locator(".candidate")).to_have_count(2)
        # Hide from the enlarged view. Keep another scene's adoption untouched.
        other.locator('[data-action="preview"]').click()
        page.locator("#review-visibility").click()
        expect(page.locator("#review-candidate-id option")).to_have_count(1)
        page.locator('[data-close="preview-dialog"]').click()
        page.locator("#undo").click()
        expect(scene.locator(".candidate")).to_have_count(2)
        adopted.locator('[data-action="clear"]').click()
        adopted.locator('[data-action="comments"]').click()
        expect(page.locator("#review-title")).to_have_text("画像のコメント")
        page.locator("#review-visibility").click()
        expect(page.locator("#review-candidate-id option")).to_have_count(1)
        # Hide the last visible candidate and recover without leaving the dialog.
        page.locator("#review-visibility").click()
        expect(page.locator("#review-grid")).to_be_hidden()
        expect(page.locator("#review-show-hidden")).to_be_visible()
        page.locator("#review-show-hidden").click()
        expect(page.locator("#review-candidate-id option")).to_have_count(2)
        expect(page.locator("#review-adopt-next")).to_be_hidden()
        expect(page.locator("#review-visibility")).to_have_text("候補に戻す")
        page.screenshot(path=str(output / f"restore-{width}.png"))
        page.locator("#review-visibility").click()
        expect(page.locator("#review-visibility")).to_have_text("候補から非表示")
        page.locator('[data-close="preview-dialog"]').click()
        # Hiding is shared between sessions. A new browser defaults to the visible candidate.
        second = context.new_page()
        second.goto(f"{origin}/materials/jobs/{job}")
        expect(second.locator('[data-no="1"] .candidate')).to_have_count(1)
        second.close()
        assert store.load_library(d)["selections"].get("2")
        for path, content in original_files.items():
            assert path.read_bytes() == content
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert not refused and not errors, (refused, errors)
        print(f"Chrome {width}px: hide, restore, undo, reload, comparison, comments, all-hidden recovery passed", flush=True)
    finally:
        context.close()


def main(output):
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="visibility-browser-", dir=output) as tmp:
        root = Path(tmp)
        with patch.dict(os.environ, {"DATA_DIR": tmp, "APP_PASSWORD": "", "CF_ACCESS_AUD": "",
                                    "RENDER_SERVICE_ID": "", "SECRET_KEY": "isolated-visibility-fixture"}):
            import app
            with patch.object(app, "APP_PASSWORD", ""), patch.object(app, "resolve_channel_keys", return_value={k: "" for k in ("openai", "gemini", "anthropic")}):
                for width in [390, 1366]:
                    seed(root, f"visibility{width}")
                logging.getLogger("werkzeug").setLevel(logging.ERROR)
                server = make_server("127.0.0.1", 0, app.app, threaded=True)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(channel="chrome", headless=True, args=["--disable-background-networking"])
                        try:
                            for width in [390, 1366]:
                                exercise(browser, f"http://127.0.0.1:{server.server_port}", root, width, output)
                        finally:
                            browser.close()
                finally:
                    server.shutdown()
                    worker.join()
                    server.server_close()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
