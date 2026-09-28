import io
import json
import threading
import time
from unittest.mock import Mock

import pytest

from deploy_guard import (CONTROL_PATH, UPSTREAM_SETTLE_SECONDS, DeployGuard, interrupt_orphaned_jobs,
                          parse_ref_advertisement, signature)

SHA = "a" * 40


@pytest.fixture
def guard(tmp_path):
    def app(env, start):
        start("200 OK", [])
        return [b"ok"]
    return DeployGuard(app, tmp_path, "test-only", "b" * 40, "old",
                       Mock(return_value=False), hook="test-hook", clock=lambda: 1000,
                       trigger=Mock())


def request(guard, method="POST", path="/start", **extra):
    statuses = []
    body = b"".join(guard({"REQUEST_METHOD": method, "PATH_INFO": path, **extra},
                         lambda status, headers: statuses.append(status)))
    return statuses[0], body


def test_signed_control_rejects_password_session_and_stale_requests(guard):
    body = json.dumps({"target": SHA}).encode()
    for stamp, secret in [(1000, "wrong"), (900, "test-only")]:
        status, _ = request(guard, path=CONTROL_PATH, CONTENT_LENGTH=str(len(body)),
            **{"wsgi.input": io.BytesIO(body), "HTTP_X_DEPLOY_TIME": str(stamp),
               "HTTP_X_DEPLOY_SIGNATURE": signature(secret, stamp, body)})
        assert status == "403 Forbidden"
    assert not guard.file.exists()
    status, result = request(guard, path=CONTROL_PATH, CONTENT_LENGTH=str(len(body)),
        **{"wsgi.input": io.BytesIO(body), "HTTP_X_DEPLOY_TIME": "1000",
           "HTTP_X_DEPLOY_SIGNATURE": signature("test-only", "1000", body)})
    assert status == "200 OK"
    assert json.loads(result)["ready"]


def test_idle_is_sealed_until_matching_replacement(guard):
    assert guard.drain(SHA)["ready"]
    assert request(guard)[0] == "503 Service Unavailable"
    assert request(guard, "GET")[0] == "200 OK"
    assert request(guard, path="/login")[0] == "200 OK"
    # Old worker restart or rollback must not reopen production accidentally.
    for commit, instance in [(SHA, "old"), ("b" * 40, "new")]:
        DeployGuard(guard.application, guard.root, "test-only", commit, instance, lambda: False)
        assert guard.file.exists()
    DeployGuard(guard.application, guard.root, "test-only", SHA, "new", lambda: False)
    assert not guard.file.exists()


def test_generation_waits_without_render_timeout_then_retries_once(guard):
    guard.jobs_busy.return_value = True
    assert not guard.drain(SHA)["ready"]
    guard.clock = lambda: 10000  # Longer than Render's 30-minute pre-deploy limit.
    guard.tick()
    guard.trigger.assert_not_called()
    guard.jobs_busy.return_value = False
    guard.tick()
    guard.tick()
    guard.trigger.assert_called_once_with("test-hook")
    assert guard._read()["phase"] == "sealed"
    assert request(guard)[0] == "503 Service Unavailable"


def test_synchronous_regeneration_and_background_tail_are_protected(guard):
    entered, release = threading.Event(), threading.Event()
    def app(env, start):
        entered.set()
        assert release.wait(5)
        start("200 OK", [])
        return [b"ok"]
    guard.application = app
    worker = threading.Thread(target=lambda: request(guard))
    worker.start()
    assert entered.wait(5)
    assert not guard.drain(SHA)["ready"]
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    entered.clear()
    release.clear()
    @guard.background_task
    def pipeline_tail():
        entered.set()
        assert release.wait(5)
    worker = threading.Thread(target=pipeline_tail)
    worker.start()
    assert entered.wait(5)
    assert not guard.drain(SHA)["ready"]
    release.set()
    worker.join(5)
    assert guard.drain(SHA)["ready"]


def test_job_io_errors_and_invalid_state_fail_closed(guard):
    guard.jobs_busy.side_effect = ValueError("partial JSON write")
    assert not guard.drain(SHA)["ready"]
    guard.file.write_text("{incomplete", encoding="utf-8")
    assert request(guard)[0] == "503 Service Unavailable"
    assert not guard.drain(SHA)["ready"]


def test_failed_hook_is_bounded_and_never_reopens_writes(guard):
    guard.jobs_busy.return_value = True
    guard.drain(SHA)
    guard.jobs_busy.return_value = False
    guard.trigger.side_effect = RuntimeError("sensitive hook must not be logged")
    for now in [1100, 2000, 3000, 4000]:
        guard.clock = lambda: now
        guard.tick()
    assert guard.trigger.call_count == 3
    assert guard._read()["phase"] == "error"
    assert request(guard)[0] == "503 Service Unavailable"


def test_newer_successful_build_replaces_pending_commit(guard):
    guard.jobs_busy.return_value = True
    guard.drain(SHA)
    guard.jobs_busy.return_value = False
    newer = "c" * 40
    assert guard.drain(newer)["ready"]
    assert guard._read()["target"] == newer


def test_iteration_error_releases_request_counter(guard):
    def app(env, start):
        raise RuntimeError("handler failed")
    guard.application = app
    with pytest.raises(RuntimeError):
        request(guard)
    assert guard.writes == 0


def test_predeploy_fails_closed_on_endpoint_error(monkeypatch):
    import urllib.request
    from deploy_guard import predeploy
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://example.onrender.com")
    monkeypatch.setenv("RENDER_GIT_COMMIT", SHA)
    monkeypatch.setenv("SECRET_KEY", "test-only")
    opener = Mock()
    opener.open.side_effect = OSError("no network")
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: opener)
    with pytest.raises(SystemExit, match="keeping existing instance"):
        predeploy()


@pytest.mark.parametrize("status", [200, 202])
def test_hook_accepts_started_or_queued_without_ref(monkeypatch, status):
    import urllib.request
    from deploy_guard import call_hook
    response = Mock(status=status)
    opener = Mock()
    opener.open.return_value.__enter__ = Mock(return_value=response)
    opener.open.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: opener)
    call_hook("https://api.render.com/deploy/srv-test?key=fixture")
    with pytest.raises(ValueError):
        call_hook("https://api.render.com/deploy/srv-test?key=fixture&ref=" + SHA)


def test_jobs_left_running_by_a_stopped_process_are_interrupted_at_startup(tmp_path):
    # 9/27: メモリ不足で落ちた回が running のまま残り、更新も新しい回も止まった
    for name, status in [("a", "running"), ("b", "completed"), ("c", "queued"), ("d", "error")]:
        (tmp_path / name).mkdir()
        (tmp_path / name / "job.json").write_text(json.dumps({"status": status, "title": name}),
                                                   encoding="utf-8")
    (tmp_path / "e").mkdir()
    (tmp_path / "e" / "job.json").write_text("{broken", encoding="utf-8")
    assert interrupt_orphaned_jobs(tmp_path, clock=lambda: 1000) == ["a", "c"]
    a = json.loads((tmp_path / "a" / "job.json").read_text(encoding="utf-8"))
    assert a["status"] == "interrupted" and a["title"] == "a" and "再開" in a["message"]
    assert json.loads((tmp_path / "b" / "job.json").read_text(encoding="utf-8"))["status"] == "completed"
    assert (tmp_path / "e" / "job.json").read_text(encoding="utf-8") == "{broken"


# ---- 2026-09-28 新しいコミットの見張り（Render の Auto-Deploy を切り、生成の合間に本番が自分で配置する）
# push のたびに Render が配置を始めると、生成中は Pre-deploy が保留で終わり「Deploy failed」のメールになっていた。

def pkt(line):
    return b"%04x" % (len(line) + 4) + line


def test_ref_advertisement_reads_the_branch_head():
    data = (pkt(b"# service=git-upload-pack\n") + b"0000"
            + pkt(b"a" * 40 + b" HEAD\x00multi_ack thin-pack symref=HEAD:refs/heads/main\n")
            + pkt(b"c" * 40 + b" refs/heads/main\n") + pkt(b"d" * 40 + b" refs/heads/dev\n") + b"0000")
    assert parse_ref_advertisement(data, "refs/heads/main") == "c" * 40
    assert parse_ref_advertisement(data, "refs/heads/dev") == "d" * 40
    assert parse_ref_advertisement(data, "refs/heads/none") is None
    assert parse_ref_advertisement(b"zzzz", "refs/heads/main") is None
    assert parse_ref_advertisement(pkt(b"x" * 40 + b" refs/heads/main\n"), "refs/heads/main") is None


@pytest.fixture
def watcher(tmp_path):
    now, heads = [1000], ["b" * 40]
    def app(env, start):
        start("200 OK", [])
        return [b"ok"]
    guard = DeployGuard(app, tmp_path, "test-only", "b" * 40, "old", Mock(return_value=False),
                        hook="test-hook", clock=lambda: now[0], trigger=Mock(),
                        upstream=("owner/repo", "main"), fetch_head=lambda: heads[0])
    return guard, now, heads


def step(guard, now, seconds=60):
    now[0] += seconds
    return guard.check_upstream()


def test_new_commit_is_deployed_once_after_settling_while_idle(watcher):
    guard, now, heads = watcher
    assert guard.check_upstream() is None          # 動いている版と同じ
    heads[0] = SHA
    assert guard.check_upstream() is None          # 初めて見た。続けて push されるかもしれない
    assert step(guard, now) is None                 # まだ落ち着いていない
    assert step(guard, now, UPSTREAM_SETTLE_SECONDS) == SHA
    guard.trigger.assert_called_once_with("test-hook")
    assert step(guard, now) is None and step(guard, now, 600) is None   # 同じコミットは1回だけ
    assert guard.trigger.call_count == 1
    assert not guard.file.exists()                  # 受付は止めない（止めるのは Pre-deploy の時だけ）
    assert request(guard)[0] == "200 OK"


def test_burst_of_pushes_deploys_only_the_last(watcher):
    guard, now, heads = watcher
    for head in ["c" * 40, "d" * 40, "e" * 40]:
        heads[0] = head
        assert step(guard, now) is None
    assert step(guard, now, UPSTREAM_SETTLE_SECONDS) == "e" * 40
    assert guard.trigger.call_count == 1


def test_waits_for_generation_to_finish_without_failing(watcher):
    guard, now, heads = watcher
    guard.jobs_busy.return_value = True
    heads[0] = SHA
    guard.check_upstream()
    for _ in range(30):                             # 生成中は30分でも呼ばない
        assert step(guard, now) is None
    guard.trigger.assert_not_called()
    guard.jobs_busy.return_value = False
    assert step(guard, now) == SHA


def test_skips_while_a_deployment_is_in_progress_or_needs_review(watcher):
    guard, now, heads = watcher
    assert guard.drain("c" * 40)["ready"]           # Pre-deploy が来て封印済み（配置の途中）
    heads[0] = SHA
    guard.check_upstream()
    assert step(guard, now, UPSTREAM_SETTLE_SECONDS) is None
    guard.file.write_text("{broken", encoding="utf-8")   # 状態が読めない＝人の確認待ち
    assert step(guard, now, UPSTREAM_SETTLE_SECONDS) is None
    guard.trigger.assert_not_called()


def test_rollback_on_render_is_not_undone(tmp_path):
    # 最新（c）が動いた後、Render で古い版（b）へ戻した。見張りは c を配置し直さない
    DeployGuard(lambda e, s: [], tmp_path, "test-only", "c" * 40, "i1", lambda: False,
                hook="h", upstream=("owner/repo", "main"), fetch_head=lambda: "c" * 40)
    now = [1000]
    old = DeployGuard(lambda e, s: [], tmp_path, "test-only", "b" * 40, "i2", lambda: False,
                      hook="h", clock=lambda: now[0], trigger=Mock(),
                      upstream=("owner/repo", "main"), fetch_head=lambda: "c" * 40)
    for _ in range(5):
        now[0] += UPSTREAM_SETTLE_SECONDS
        assert old.check_upstream() is None
    old.trigger.assert_not_called()


def test_hook_failures_are_bounded_and_network_errors_are_quiet(watcher):
    guard, now, heads = watcher
    heads[0] = SHA
    guard.trigger.side_effect = RuntimeError("sensitive hook must not be logged")
    guard.check_upstream()
    now[0] += UPSTREAM_SETTLE_SECONDS
    for _ in range(6):
        assert step(guard, now) is None
    assert guard.trigger.call_count == 3
    def offline():
        raise OSError("no network")
    guard.fetch_head = offline
    assert step(guard, now) is None


def test_disabled_without_hook_or_upstream(tmp_path):
    fetch = Mock(return_value=SHA)
    for kwargs in [dict(hook="", upstream=("owner/repo", "main")), dict(hook="h", upstream=None)]:
        guard = DeployGuard(lambda e, s: [], tmp_path, "test-only", "b" * 40, "i", lambda: False,
                            trigger=Mock(), fetch_head=fetch, **kwargs)
        assert guard.check_upstream() is None
    fetch.assert_not_called()

