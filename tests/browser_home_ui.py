"""Optional Chrome rehearsal of entry/history UI; synthetic records and no write APIs."""
import json
import logging
import sys
import threading
from pathlib import Path

from flask import Flask, render_template, request
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
app = Flask(__name__, template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static"))
BOARD = dict(running=None, waiting=[], other_operations=0, accepting=True)
CHANNELS = [dict(id="economy", source_id="keizai", name="日本カラクリ経済学", color="#087f73", mark="経", logo="channel-logos/keizai.png", description="経済のしくみを、身近な言葉と図で。", ready=True, styles=[]),
            dict(id="russia", source_id="roshia", name="ロシア解体新書", color="#7553c8", mark="露", logo="channel-logos/roshia.png", description="暮らし・歴史・社会を読み解く。", ready=True, styles=[]),
            dict(id="otona", source_id="otona", name="大人の学び直しTV", color="#cc7152", mark="学", logo="channel-logos/otona-suasi.jpg", description="知るほど、世界はおもしろい。", ready=True, styles=[])]


@app.get("/materials")
def home():
    rows = [dict(id=f"sample-{i:03}", project_id=f"project-{i}", title=f"確認用 {i:02}｜価格と暮らしのしくみ", date="2026/11/10 18:00",
                 tone="success" if i % 2 == 0 else "attention", status="completed" if i % 2 == 0 else "interrupted",
                 status_label="生成済み" if i % 2 == 0 else "中断", bucket="completed" if i % 2 == 0 else "unfinished",
                 source_label="素材スタジオ", generated=3, total=12, selected=1, href=f"/materials/jobs/sample-{i:03}",
                 action="候補を確認", resumable=False, trello_url="") for i in range(1, 26)]
    if request.args.get("empty"):
        rows = []
    return render_template("materials.html", csrf="fixture", channels=CHANNELS, selected=request.args.get("channel"),
                           history=dict(jobs=rows, unfinished=13, completed=12, warning=False, drafts=[]), board=BOARD,
                           prefill_title="", prefill_trello="")


@app.get("/api/material-generation-board")
def board():
    return BOARD


def main(output):
    output.mkdir(parents=True, exist_ok=True)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    results = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            try:
                for width in [390, 1366]:
                    context = browser.new_context(viewport={"width":width, "height":900})
                    page = context.new_page()
                    errors, writes = [], []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("request", lambda req: writes.append(req.url) if req.method != "GET" else None)
                    page.goto(origin + "/materials")
                    expect(page.locator("h1")).to_contain_text("使える素材に。")
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                    page.screenshot(path=str(output / f"material-entry-{width}.png"), full_page=True)
                    page.goto(origin + "/materials?channel=economy")
                    rows = page.locator(".job-row:visible")
                    expect(rows).to_have_count(20)
                    page.locator("#history-more").click()
                    expect(rows).to_have_count(25)
                    page.locator("#history-search").fill("価格　０２｜")
                    expect(rows).to_have_count(1)
                    page.locator('[data-history-filter="completed"]').click()
                    expect(rows).to_have_count(1)
                    page.reload()
                    expect(rows).to_have_count(1)
                    expect(page.locator("#history-search")).to_have_value("価格　０２｜")
                    page.locator("#history-search").fill("存在しない言葉")
                    expect(page.locator("#history-no-match")).to_be_visible()
                    page.locator("[data-clear-history]").click()
                    expect(rows).to_have_count(20)
                    expect(page.locator('[data-history-filter="all"]')).to_have_attribute("aria-pressed", "true")
                    page.locator("h1").click()
                    page.keyboard.press("/")
                    expect(page.locator("#history-search")).to_be_focused()
                    page.locator("#history-search").fill("価格")
                    page.keyboard.press("Escape")
                    expect(page.locator("#history-search")).to_have_value("")
                    page.locator("[data-open-project]").first.click()
                    expect(page.locator("#new-project-dialog")).to_be_visible()
                    page.keyboard.press("Escape")
                    expect(page.locator("#new-project-dialog")).not_to_be_visible()
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                    page.locator("#production-history").scroll_into_view_if_needed()
                    page.screenshot(path=str(output / f"material-history-{width}.png"))
                    page.goto(origin + "/materials?channel=economy&empty=1")
                    expect(page.get_by_text("最初の素材を作りましょう")).to_be_visible()
                    assert not errors and not writes, (errors, writes)
                    results.append(dict(width=width, history=25, writes=0, errors=0, checks="multiword/fullwidth/filter/reset/restore/keyboard/dialog/empty/overflow"))
                    context.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    (output / "material-home-checks.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
