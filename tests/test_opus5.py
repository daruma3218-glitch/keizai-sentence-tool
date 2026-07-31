#!/usr/bin/env python3
"""Opus 5 移植の pytest。

受け入れ基準:
- 品質クリティカル工程だけが claude-opus-5 になる:
  - prompter（画像プロンプト・図解blueprint）→ opus-5（PROMPTER_MODEL で上書き可）
  - router の chart/map 抽出 → opus-5（EXTRACT_MODEL で上書き可）
- 件数の多い分類系は据え置き: ルート判定/分解/Web検索= sonnet-5、検品= haiku
- 実際の claude_query 呼び出しに model が渡ること
"""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import prompter  # noqa: E402
import router  # noqa: E402
import splitter  # noqa: E402
import verifier  # noqa: E402
import web_searcher  # noqa: E402


def test_model_assignment_per_stage():
    assert prompter.CLAUDE_MODEL == "claude-opus-5", "図解設計・プロンプト生成は Opus 5"
    assert router.EXTRACT_MODEL == "claude-opus-5", "chart/map 抽出は Opus 5"
    assert router.CLAUDE_MODEL == "claude-sonnet-5", "ルート判定は Sonnet 据え置き"
    assert splitter.CLAUDE_MODEL == "claude-sonnet-5", "分解は Sonnet 据え置き"
    assert web_searcher.CLAUDE_MODEL == "claude-sonnet-5", "Web検索は Sonnet 据え置き"
    assert verifier.CLAUDE_MODEL == "claude-haiku-4-5", "検品は Haiku 据え置き"


def test_prompter_batch_calls_opus5(monkeypatch):
    captured = {}

    def fake_q(client, query, system, **kw):
        captured.update(kw)
        return "[]"
    monkeypatch.setattr(prompter, "claude_query", fake_q)
    prompter.generate_prompts_batch(
        None, [{"no": 1, "sentence": "テスト文。", "type": "illustration"}], title="T")
    assert captured["model"] == "claude-opus-5"


def test_chart_and_map_extract_call_opus5(monkeypatch):
    models = []

    def fake_q(client, query, system, **kw):
        models.append(kw.get("model"))
        return "[]"
    monkeypatch.setattr(router, "claude_query", fake_q)
    router.extract_chart_specs(
        None, [{"no": 1, "sentence": "GDPは500兆円から600兆円に増えた。"}],
        log=lambda *a, **k: None)
    router.extract_map_specs(
        None, [{"no": 2, "sentence": "ロシアからドイツへガスが輸出された。"}],
        log=lambda *a, **k: None)
    assert models and all(m == "claude-opus-5" for m in models), models


def test_prompter_model_env_override(monkeypatch):
    monkeypatch.setenv("PROMPTER_MODEL", "claude-sonnet-5")
    try:
        m = importlib.reload(prompter)
        assert m.CLAUDE_MODEL == "claude-sonnet-5", "環境変数で従来モデルに戻せる"
    finally:
        monkeypatch.delenv("PROMPTER_MODEL", raising=False)
        importlib.reload(prompter)  # 他テストへ影響しないよう既定へ復元
