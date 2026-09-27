"""グラフの AI清書（chart_restyle.py）と、図解の先生にも参照画像を渡す修正（2026-09-27）。"""
import asyncio
import base64
import io
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

import chart_restyle
from chart_restyle import normalize_numbers, numbers_match, restyle_chart_file
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
    def __init__(self, first, second):
        self.messages = SimpleNamespace(create=lambda **kw: SimpleNamespace(
            content=[SimpleNamespace(text='{"first": %s, "second": %s}' % (first, second))]))


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
        reference_path=str(ref))
    assert res["restyled"] is True
    assert (images / "5.png").read_bytes() != original
    # 元のグラフは images/ の外に残し、スタッフの ZIP に混ざらない
    assert (tmp_path / "chart_render" / "5.png").read_bytes() == original
    assert sorted(p.name for p in images.iterdir()) == ["5.png"]
    # グラフと先生の2枚を渡し、世界観の設定文を足す
    assert len(calls[0]["image"]) == 2
    assert "CHANNEL WORLD" in calls[0]["prompt"]


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
    by_prompt = {p: u for u, p in seen}
    with_prof = next(u for u, p in seen if "professor stands" in p)
    without = next(u for u, p in seen if "coins and arrows" in p)
    assert with_prof is True and without is False
    assert any("Draw him at most once" in p for u, p in seen if u)
    assert by_prompt  # 2枚とも生成された
