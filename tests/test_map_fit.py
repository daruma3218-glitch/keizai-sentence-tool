"""地名が出るだけで地図にしない（2026-10-14・図解スタジオと同じ判断）。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import router


def route_of(sentence):
    return router._fallback_route_for_row({"sentence": sentence, "chapter_title": "", "block_text": ""})["route"]


def test_country_or_city_names_alone_do_not_make_a_map():
    assert route_of("フランスの首脳はロシアの大統領と会談を行いました") == "web_photo"
    assert route_of("エストニア政府は首都タリンの記念碑を郊外の墓地へ移しました") != "map"
    assert route_of("ロシアの実業家たちは静かに様子をうかがっていました") != "map"


def test_location_relations_still_make_a_map():
    assert route_of("リトアニアはカリーニングラード州とべったり接しています") == "map"
    assert route_of("この細長い回廊は、ロシアの飛び地とベラルーシに挟まれた陸路です") == "map"
    assert route_of("三つの国は国境沿いに長い防衛線を築くことで合意しました") == "map"


def test_router_rules_explain_when_a_map_is_right():
    source = Path(router.__file__).read_text(encoding="utf-8")
    assert "地名・国名・都市名が出てくるだけでは map にしない" in source
    assert "昔の国・昔の国境" in source
