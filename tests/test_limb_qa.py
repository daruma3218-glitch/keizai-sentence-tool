"""人物イラストの腕・手の検品（limb_qa.py・2026-09-27 社長「腕が3本…こういうミスがおきないように」）。"""
import json

from PIL import Image

from limb_qa import JUDGES, check_limbs, combine, limb_verdict

THREE_ARMS = {"figures": [{"who": "先生", "hands": [
    {"box_2d": [300, 700, 380, 760], "attached_to": "右肩"},   # あごの手
    {"box_2d": [600, 560, 660, 640], "attached_to": "左肩"},   # 左に開いた手
    {"box_2d": [600, 800, 660, 880], "attached_to": "右肩"},   # 右に開いた手
]}], "other_problems": []}
TWO_ARMS = {"figures": [{"who": "先生", "hands": [
    {"box_2d": [300, 700, 380, 760], "attached_to": "右肩"},
    {"box_2d": [600, 560, 660, 640], "attached_to": "左肩"},
]}, {"who": "社員", "hands": [{"box_2d": [700, 200, 760, 260], "attached_to": "右肩"}]}],
    "other_problems": []}


def test_three_hands_fail():
    v = limb_verdict(THREE_ARMS)
    assert v["ok"] is False
    assert any("手が3つ" in p for p in v["problems"])
    assert any("同じ肩" in p for p in v["problems"])


def test_normal_people_pass():
    assert limb_verdict(TWO_ARMS)["ok"] is True


def test_arm_from_chest_fails():
    data = {"figures": [{"who": "先生", "hands": [{"box_2d": [500, 500, 560, 560], "attached_to": "胸・胴の途中"}]}]}
    assert limb_verdict(data)["ok"] is False


def test_duplicate_detection_of_one_hand_is_merged():
    data = {"figures": [{"who": "先生", "hands": [
        {"box_2d": [300, 700, 380, 760], "attached_to": "右肩"},
        {"box_2d": [310, 705, 385, 765], "attached_to": "右肩"},   # 同じ手の二重検出
        {"box_2d": [600, 560, 660, 640], "attached_to": "左肩"}]}]}
    assert limb_verdict(data)["ok"] is True


def test_one_failing_judge_is_enough():
    assert combine([{"ok": True}, {"ok": False, "problems": ["先生: 手が3つある"]}])["ok"] is False
    assert combine([{"ok": True}, {"ok": None}])["ok"] is True
    assert combine([{"ok": None}, {"ok": None}])["ok"] is None


def test_check_limbs_asks_both_judges_with_images(tmp_path):
    img = tmp_path / "12.png"
    Image.new("RGB", (160, 90), (200, 200, 200)).save(img)
    ref = tmp_path / "ref.png"
    Image.new("RGB", (64, 64), (255, 0, 0)).save(ref)
    calls = []

    def fake_generate(system, prompt, **kw):
        calls.append(kw)
        data = THREE_ARMS if kw["model"] == "claude-opus-5-5" else TWO_ARMS
        return json.dumps(data, ensure_ascii=False), {}

    result = check_limbs(img, str(ref), generate=fake_generate)
    assert result["ok"] is False  # Opus だけが見つけても不合格
    assert sorted(c["model"] for c in calls) == sorted(m for m, _ in JUDGES)
    assert all(len(c["attachments"]) == 2 and c["allow_fallback"] is False for c in calls)
    assert all(c["tool"] == "sentence" for c in calls)  # PC の窓口が許可している用途名


def test_prompt_limits_problems_to_limbs_not_looks():
    # 9/27 本番: 眼鏡のない一般の人物を「先生の眼鏡がない」として不合格にした（№19・20・33）
    from limb_qa import PROMPT
    assert "眼鏡の有無" in PROMPT and "一般の人物として扱う" in PROMPT


class _Stopped(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason
        self.attempts = [{"provider": "claude", "attempt": 1, "reason": reason, "elapsed_s": 42.0}]


def _images(tmp_path):
    img = tmp_path / "12.png"
    Image.new("RGB", (160, 90), (200, 200, 200)).save(img)
    return img


def test_one_judge_failing_is_silent_and_other_judge_decides(tmp_path):
    # 2026-09-28: Opus だけ止まった時に停止通知が出ていた（20260928_023118）。もう一方で合否が出るので通知しない
    calls, groups = [], []

    def fake_generate(system, prompt, **kw):
        calls.append(kw)
        if kw["model"] == "claude-opus-5-5":
            raise _Stopped("primary_cli_unavailable")
        return json.dumps(TWO_ARMS, ensure_ascii=False), {}

    result = check_limbs(_images(tmp_path), generate=fake_generate, notify_group=lambda **kw: groups.append(kw))
    assert result["ok"] is True
    assert all(c["notify"] is False for c in calls)
    assert groups == []
    assert all("_attempts" not in v for v in result["judges"])


def test_both_judges_failing_sends_one_group_notice(tmp_path):
    groups = []

    def fake_generate(system, prompt, **kw):
        raise _Stopped("primary_cli_unavailable")

    result = check_limbs(_images(tmp_path), job_id="job-7", generate=fake_generate,
                         notify_group=lambda **kw: groups.append(kw))
    assert result["ok"] is None
    assert len(groups) == 1
    assert groups[0]["job_id"] == "job-7" and groups[0]["reason"] == "all_judges_failed"
    assert len(groups[0]["attempts"]) == 2
