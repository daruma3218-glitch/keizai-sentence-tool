"""画面・通知・出力に出る道具の名前は「素材スタジオ」（2026-10-11 社長「センテンスも内容の通り素材スタジオに名称なりURLも切り替えておきたい」）。

文のことを指す「センテンス」や、旧方式の案件を指す「従来のセンテンス」はそのまま。内部の名前（URL の道・保存先・ワーカーの道具名）も変えない。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD = "センテンスつくーる"


def test_templates_do_not_show_the_old_tool_name():
    hits = [p.name for p in (ROOT / "templates").glob("*.html") if OLD in p.read_text(encoding="utf-8")]
    assert hits == []


def test_notifications_and_outputs_use_the_new_name():
    for name in ("app.py", "pipeline.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        lines = [ln for ln in text.splitlines() if OLD in ln and f"旧{OLD}" not in ln]
        assert lines == [], name
    app_text = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "🤖 素材スタジオ：「{title}」ができました" in app_text
    assert "🤖 素材スタジオ：「{title}」が途中で止まりました" in app_text
