#!/usr/bin/env python3
"""内容判断のASTRA移行と、収集・分解モデルを維持する境界の検証。

受け入れ基準:
- prompter、ルート判定、chart/map抽出、意味検査はASTRA
- 文の分解とWeb検索はSonnetを維持
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
    assert prompter.CLAUDE_MODEL == "gpt-6-astra"
    assert router.EXTRACT_MODEL == "gpt-6-astra"
    assert router.CLAUDE_MODEL == "gpt-6-astra"
    assert splitter.CLAUDE_MODEL == "claude-sonnet-5", "分解は Sonnet 据え置き"
    assert web_searcher.CLAUDE_MODEL == "claude-sonnet-5", "Web検索は Sonnet 据え置き"
    assert verifier.CLAUDE_MODEL == "gpt-6-astra"


def test_prompter_batch_calls_astra_high(monkeypatch):
    captured = {}

    def fake_q(client, query, system, **kw):
        captured.update(kw)
        return "[]"
    monkeypatch.setattr(prompter, "claude_query", fake_q)
    prompter.generate_prompts_batch(
        None, [{"no": 1, "sentence": "テスト文。", "type": "illustration"}], title="T")
    assert captured["model"] == "gpt-6-astra"
    assert captured["effort"] == "high"
    assert captured["workload"] == "assets_plan"


def test_chart_and_map_extract_call_astra(monkeypatch):
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
    assert models and all(m == "gpt-6-astra" for m in models), models


def test_prompter_model_env_override(monkeypatch):
    monkeypatch.setenv("PROMPTER_MODEL", "claude-sonnet-5")
    try:
        m = importlib.reload(prompter)
        assert m.CLAUDE_MODEL == "claude-sonnet-5", "環境変数で従来モデルに戻せる"
    finally:
        monkeypatch.delenv("PROMPTER_MODEL", raising=False)
        importlib.reload(prompter)  # 他テストへ影響しないよう既定へ復元
