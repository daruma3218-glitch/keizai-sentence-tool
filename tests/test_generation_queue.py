"""順番待ち（生成は一度に1回だけ）と完了の知らせ（2026-09-28 社長「誰かが作業している時に分かるように」）。"""
import threading
import time

import pytest

import app as appmod


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(appmod, "_QUEUE", [])
    monkeypatch.setattr(appmod, "_RUNNING", {"job_id": None})
    monkeypatch.setattr(appmod, "_GENERATION_SLOT", threading.Lock())
    monkeypatch.setattr(appmod, "_notify_job_done", lambda job_id: None)
    return tmp_path


def test_second_job_waits_until_the_first_finishes(isolated, monkeypatch):
    order = []
    release_first = threading.Event()

    def body(job_id, *a, **k):
        order.append(("start", job_id))
        if job_id == "20260928_100000":
            release_first.wait(5)
        order.append(("end", job_id))

    monkeypatch.setattr(appmod, "_run_pipeline_body", body)
    for jid in ("20260928_100000", "20260928_100100"):
        (isolated / jid).mkdir()
        appmod._set_job_state(jid, status="queued", title=f"回{jid[-4:]}", creator="新居", channel_id="keizai")
    t1 = threading.Thread(target=appmod._run_pipeline_thread, args=("20260928_100000",))
    t1.start()
    time.sleep(0.3)
    t2 = threading.Thread(target=appmod._run_pipeline_thread, args=("20260928_100100",))
    t2.start()
    time.sleep(0.5)
    # 2件目は順番待ち（いま作成中の回と作る人が出る）
    st = appmod._get_job_state("20260928_100100")
    assert st["status"] == "queued" and "前に 1 件" in st["message"] and "新居さん" in st["message"]
    board = appmod.generation_board()
    assert board["running"]["id"] == "20260928_100000"
    assert [w["id"] for w in board["waiting"]] == ["20260928_100100"]
    assert ("start", "20260928_100100") not in order
    release_first.set()
    t1.join(5)
    t2.join(5)
    assert order == [("start", "20260928_100000"), ("end", "20260928_100000"),
                     ("start", "20260928_100100"), ("end", "20260928_100100")]
    assert appmod.generation_board() == {"running": None, "waiting": []}


def test_done_message_counts_flagged_images_and_mentions_next_job():
    state = {"status": "completed", "title": "ステルス値上げ", "creator": "新居",
             "generated": 38, "total_sentences": 45}
    rows = [{"verify_issue": True}, {"verify_issue": False}, {"verify_issue": True}]
    body = appmod._job_done_message("20260928_100000", state, rows,
                                    {"title": "ルノアール", "creator": "安福"})
    assert "「ステルス値上げ」ができました（新居さん）" in body
    assert "画像 38 枚（全 45 文）・要確認⚠ 2 枚" in body
    assert "/progress/20260928_100000" in body
    assert "順番待ちの「ルノアール」（安福さん）を始めます" in body


def test_stopped_job_message_points_to_resume():
    body = appmod._job_done_message("j", {"status": "error", "title": "t", "message": "メモリ不足"}, [], None)
    assert "途中で止まりました" in body and "再開" in body


_REAL_NOTIFY = appmod._notify_job_done


def test_no_notice_without_token(isolated, monkeypatch):
    monkeypatch.delenv("CHATWORK_API_TOKEN", raising=False)
    (isolated / "j1").mkdir()
    appmod._set_job_state("j1", status="completed", channel_id="keizai", title="t")
    assert _REAL_NOTIFY("j1") is False  # 鍵が無ければ送らない（生成は止めない）


def test_notice_goes_to_the_channel_room(isolated, monkeypatch):
    monkeypatch.setenv("CHATWORK_API_TOKEN", "dummy")
    (isolated / "j2").mkdir()
    appmod._set_job_state("j2", status="completed", channel_id="keizai", title="t", generated=3,
                          total_sentences=3)
    sent = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        sent["url"], sent["data"] = req.full_url, req.data.decode("utf-8")
        return FakeResponse()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert _REAL_NOTIFY("j2") is True
    room = appmod.get_channel("keizai")["defaults"]["chatwork_room_id"]
    assert sent["url"] == f"https://api.chatwork.com/v2/rooms/{room}/messages"
    assert "body=" in sent["data"]


def test_running_job_is_not_notified(isolated, monkeypatch):
    monkeypatch.setenv("CHATWORK_API_TOKEN", "dummy")
    (isolated / "j3").mkdir()
    appmod._set_job_state("j3", status="running", channel_id="keizai", title="t")
    assert _REAL_NOTIFY("j3") is False
