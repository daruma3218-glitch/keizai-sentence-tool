"""グラフの AI清書（chart_restyle.py）と、図解の先生にも参照画像を渡す修正（2026-09-27）。"""
import asyncio
import base64
import io
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

import chart_restyle
from chart_restyle import normalize_numbers, numbers_match, restyle_chart_file, texts_match
from generator import ParallelImageGenerator, depicts_recurring_character, PROVIDER_GPT_IMAGE


def _png_bytes(color=(200, 200, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (160, 90), color).save(buf, format="PNG")
    return buf.getvalue()


# ── 数字の突き合わせ
def test_numbers_are_compared_without_units_and_separators():
    assert normalize_numbers(["100グラム", "1,000円", "５５", "2.50%"]) == sorted(["100", "1000", "55", "2.5"])
    assert numbers_match(["100グラム", "1984年"], ["1984", "100"]) == (True, "")


def test_missing_or_extra_numbers_are_rejected():
    ok, why = numbers_match(["100", "55"], ["100"])
    assert not ok and "55" in why
    ok, why = numbers_match(["100", "55"], ["100", "55", "120"])
    assert not ok and "120" in why


def test_decimals_keep_their_leading_zero():
    assert normalize_numbers(["0.5%", "0.50"]) == ["0.5", "0.5"]


def test_reworded_or_added_labels_are_rejected():
    # 9/27 の試験で実際に起きた言い換え・追加
    ok, why = texts_match(["一袋あたりの受取額と費用", "メーカーの受取額"],
                          ["一袋あたりの収入と費用（仮定）", "メーカーが受け取るお金"])
    assert not ok and "仮" in why
    ok, _ = texts_match(["企業に残る利益"], ["手元に残る利益"])
    assert not ok


def test_same_labels_split_differently_still_match():
    assert texts_match(["内容量の変化", "100グラム vs 55グラム"],
                       ["内容量の", "変化", "100グラム", "VS", "55グラム", "¥"]) == (True, "")


def test_no_numbers_in_original_is_not_a_match():
    assert numbers_match([], [])[0] is False


# ── 描き直しの流れ（外部 API は偽物）
class _FakeImages:
    def __init__(self, calls):
        self.calls = calls

    def edit(self, **kwargs):
        self.calls.append(kwargs)
        b64 = base64.b64encode(_png_bytes((10, 120, 200))).decode()
        return SimpleNamespace(data=[SimpleNamespace(b64_json=b64)])


class _FakeVerify:
    def __init__(self, first, second, first_texts='["内容量"]', second_texts='["内容量"]'):
        body = '{"first": %s, "second": %s, "first_texts": %s, "second_texts": %s}' % (
            first, second, first_texts, second_texts)
        self.messages = SimpleNamespace(create=lambda **kw: SimpleNamespace(
            content=[SimpleNamespace(text=body)]))


def _job(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    (images / "5.png").write_bytes(_png_bytes())
    ref = tmp_path / "ref.png"
    ref.write_bytes(_png_bytes((255, 0, 0)))
    return images, ref


def test_restyle_replaces_chart_when_numbers_match(tmp_path):
    images, ref = _job(tmp_path)
    original = (images / "5.png").read_bytes()
    calls = []
    res = restyle_chart_file(
        images, 5, openai_client=SimpleNamespace(images=_FakeImages(calls)),
        verify_client=_FakeVerify('["100", "55"]', '["100グラム", "55グラム"]'),
        model="gpt-image-2.5-flare", quality="medium", style_lock_text="CHANNEL WORLD",
        reference_path=str(ref), sentence="カルビーのポテトチップス、うすしお味です。")
    assert res["restyled"] is True
    assert (images / "5.png").read_bytes() != original
    # 元のグラフは images/ の外に残し、スタッフの ZIP に混ざらない
    assert (tmp_path / "chart_render" / "5.png").read_bytes() == original
    assert sorted(p.name for p in images.iterdir()) == ["5.png"]
    # グラフと先生の2枚を渡し、世界観の設定文を足す
    assert len(calls[0]["image"]) == 2
    assert "CHANNEL WORLD" in calls[0]["prompt"]
    assert "ポテトチップス" in calls[0]["prompt"]  # 話題に合う絵を選べるよう文を渡す


def test_restyle_keeps_code_chart_when_numbers_differ(tmp_path):
    images, ref = _job(tmp_path)
    original = (images / "5.png").read_bytes()
    res = restyle_chart_file(
        images, 5, openai_client=SimpleNamespace(images=_FakeImages([])),
        verify_client=_FakeVerify('["100", "55"]', '["100", "50"]'),
        model="gpt-image-2.5-flare", quality="medium", reference_path=str(ref))
    assert res["restyled"] is False and "55" in res["reason"]
    assert (images / "5.png").read_bytes() == original
    assert not (tmp_path / "chart_render" / "5_ai.png").exists()


# ── 図解・グラフに先生が出るときも参照画像を渡す
def test_professor_in_diagram_is_detected_only_under_style_lock():
    assert depicts_recurring_character("the professor points at the arrow", "diagram", "WORLD")
    assert depicts_recurring_character("先生が図を指さす", "chart", "WORLD")
    assert not depicts_recurring_character("the professor points", "diagram", "")
    assert not depicts_recurring_character("the professor", "realphoto", "WORLD")
    assert not depicts_recurring_character("a factory and coins", "diagram", "WORLD")


def test_generator_attaches_reference_to_diagram_with_professor(tmp_path):
    ref = tmp_path / "ref.png"
    ref.write_bytes(_png_bytes())
    gen = ParallelImageGenerator(provider=PROVIDER_GPT_IMAGE, openai_api_key="sk-test",
                         reference_image_path=str(ref), style_lock_text="CHANNEL WORLD")
    seen = []

    def fake_dispatch(full_prompt, output_path, use_reference=False, *args, **kw):
        seen.append((use_reference, full_prompt))
        Path(output_path).write_bytes(_png_bytes())
        return True, ""

    gen._dispatch_sync_generate = fake_dispatch
    prompts = [
        {"index": 1, "prompt": "Diagram: price vs amount. The professor stands beside it.", "type": "diagram"},
        {"index": 2, "prompt": "Diagram: factory, coins and arrows only.", "type": "diagram"},
    ]
    asyncio.run(gen.generate_all(prompts, tmp_path))
    with_prof = next(p for u, p in seen if "professor stands" in p)
    without = next((u, p) for u, p in seen if "coins and arrows" in p)
    assert "Draw him at most once" in with_prof
    # 先生が文面に無い図解（簡易の指示文を含む）も、世界観ロック中は画風の参照として渡す
    assert without[0] is True and "STYLE REFERENCE" in without[1]
    assert all("half-lidded" in p for u, p in seen)


def test_no_reference_without_style_lock(tmp_path):
    ref = tmp_path / "ref.png"
    ref.write_bytes(_png_bytes())
    gen = ParallelImageGenerator(provider=PROVIDER_GPT_IMAGE, openai_api_key="sk-test",
                                 reference_image_path=str(ref), style_lock_text="")
    seen = []

    def fake_dispatch(full_prompt, output_path, use_reference=False, *args, **kw):
        seen.append(use_reference)
        Path(output_path).write_bytes(_png_bytes())
        return True, ""

    gen._dispatch_sync_generate = fake_dispatch
    asyncio.run(gen.generate_all([{"index": 1, "prompt": "a factory", "type": "diagram"}], tmp_path))
    assert seen == [False]


def test_restyle_keeps_code_chart_when_labels_are_reworded(tmp_path):
    images, ref = _job(tmp_path)
    original = (images / "5.png").read_bytes()
    res = restyle_chart_file(
        images, 5, openai_client=SimpleNamespace(images=_FakeImages([])),
        verify_client=_FakeVerify('["5"]', '["5"]', '["企業に残る利益"]', '["手元に残る利益"]'),
        model="gpt-image-2.5-flare", quality="medium", reference_path=str(ref))
    assert res["restyled"] is False
    assert (images / "5.png").read_bytes() == original


def test_redraw_only_draws_objects_named_in_the_sentence():
    # 9/27: 「昔は、100グラム。」（何の重さかを伏せている文）にご飯茶碗が描かれた
    from chart_restyle import CHART_RESTYLE_INSTRUCTION
    assert "only of an object that the narration sentence" in CHART_RESTYLE_INSTRUCTION
    assert "Never draw food" in CHART_RESTYLE_INSTRUCTION
