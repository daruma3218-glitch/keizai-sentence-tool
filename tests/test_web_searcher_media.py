from web_searcher import _source_type, _youtube_thumbnail_url


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
