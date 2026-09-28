"""テスト共通の設定。"""
import pytest


@pytest.fixture(autouse=True)
def _no_wikimedia_wait(monkeypatch):
    # 本番は Wikimedia へ1秒に1件（commons_searcher.WIKIMEDIA_MIN_INTERVAL）。テストでは待たない
    import commons_searcher
    monkeypatch.setattr(commons_searcher, "WIKIMEDIA_MIN_INTERVAL", 0.0)
