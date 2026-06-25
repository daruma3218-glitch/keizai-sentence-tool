import web_searcher
from web_searcher import _is_youtube_url, _source_type, _youtube_thumbnail_url


def test_youtube_thumbnail_url_from_watch_url():
    url = "https://www.youtube.com/watch?v=abc123XYZ"
    assert _youtube_thumbnail_url(url) == "https://img.youtube.com/vi/abc123XYZ/hqdefault.jpg"


def test_youtube_thumbnail_url_from_short_url():
    url = "https://youtu.be/abc123XYZ"
    assert _youtube_thumbnail_url(url) == "https://img.youtube.com/vi/abc123XYZ/hqdefault.jpg"


def test_source_type_detects_primary_media_sources():
    assert _source_type("https://www.youtube.com/watch?v=x", "Keynote") == "youtube"
    assert _source_type("https://www.whitehouse.gov/briefing-room/", "") == "official"
    assert _source_type("https://www.harvard.edu/research/example", "") == "research"
    assert _source_type("https://example.com/investor/annualreport", "") == "company"


def test_is_youtube_url_detects_youtube_domains():
    assert _is_youtube_url("https://www.youtube.com/watch?v=x")
    assert _is_youtube_url("https://youtu.be/x")
    assert not _is_youtube_url("https://www.whitehouse.gov/briefing-room/")


def test_search_single_sentence_skips_youtube_results(monkeypatch):
    def fake_call(*args, **kwargs):
        return "", [
            {"url": "https://www.youtube.com/watch?v=abc", "title": "Official lecture"},
            {"url": "https://www.whitehouse.gov/briefing-room/", "title": "Official release"},
        ]

    monkeypatch.setattr(web_searcher, "_claude_research_call", fake_call)
    result = web_searcher.search_single_sentence(
        object(),
        {"no": 1, "query": "test", "topic": "test"},
        profile="primary_media",
    )
    assert result["source_type"] == "official"
    assert "youtube.com" not in result["source_url"]


def test_search_single_sentence_returns_empty_when_only_youtube(monkeypatch):
    def fake_call(*args, **kwargs):
        return "", [{"url": "https://youtu.be/abc", "title": "Only video"}]

    monkeypatch.setattr(web_searcher, "_claude_research_call", fake_call)
    result = web_searcher.search_single_sentence(
        object(),
        {"no": 2, "query": "test", "topic": "test"},
        profile="primary_media",
    )
    assert result["source_url"] == ""
    assert result["source_type"] == "article"
    assert result["thumb_url"] == ""
