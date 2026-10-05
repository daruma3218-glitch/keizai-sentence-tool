"""Drain generation before Render replaces this single-worker service.

Only Render, using the existing server-side SECRET_KEY, can request a drain.
No project data or credentials are returned by the control endpoint.
"""
import functools
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import urllib.request

CONTROL_PATH = "/_ops/deploy/drain"
TERMINAL = {"completed", "error", "failed", "cancelled", "interrupted"}
MESSAGE = "更新を準備しています。実行中の生成が完了するまで待機し、自動で更新します。保存済みの結果は閲覧・ダウンロードできます。"
UPSTREAM_POLL_SECONDS = 60      # GitHub の main を見に行く間隔
UPSTREAM_SETTLE_SECONDS = 120   # 新しいコミットを最初に見てから配置を呼ぶまで（続けて push されたら最後の1回だけ配置する）
_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_BRANCH = re.compile(r"[A-Za-z0-9_./-]+")


def signature(secret, timestamp, body):
    message = b"sentence-deploy-v1\n" + str(timestamp).encode() + b"\n" + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def call_hook(url):
    # Never send this credential to a user-selected host or follow redirects.
    from urllib.parse import urlsplit, parse_qs
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "api.render.com"
            or not re.fullmatch(r"/deploy/srv-[a-z0-9]+", parsed.path)
            or set(parse_qs(parsed.query)) != {"key"}):
        raise ValueError("Invalid Render deploy hook configuration")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.build_opener(NoRedirect).open(req, timeout=20) as response:
        if response.status not in {200, 202}:
            raise RuntimeError("Render did not accept the deployment retry")


def parse_ref_advertisement(data, ref):
    """git の smart HTTP の参照一覧（pkt-line）から ref のコミットを返す。見つからない・壊れていれば None。"""
    pos = 0
    while pos + 4 <= len(data):
        try:
            size = int(data[pos:pos + 4], 16)
        except ValueError:
            return None
        if size == 0:  # flush-pkt
            pos += 4
            continue
        if size < 4 or pos + size > len(data):
            return None
        line = data[pos + 4:pos + size].rstrip(b"\n").split(b"\x00", 1)[0]
        pos += size
        sha, _, name = line.partition(b" ")
        if name.decode("utf-8", "replace") == ref and re.fullmatch(rb"[0-9a-f]{40}", sha):
            return sha.decode()
    return None


def remote_head(slug, branch, timeout=15):
    """公開リポジトリの branch の最新コミット。GitHub の git の入口を読む（鍵なし・REST API の回数制限を使わない）。"""
    if not _SLUG.fullmatch(slug or "") or not _BRANCH.fullmatch(branch or "") or ".." in branch:
        raise ValueError("Invalid upstream repository")
    request = urllib.request.Request(f"https://github.com/{slug}.git/info/refs?service=git-upload-pack",
                                     headers={"User-Agent": "sentence-deploy-guard"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(2_000_000)
    return parse_ref_advertisement(data, "refs/heads/" + branch)


class DeployGuard:
    def __init__(self, application, root, secret, commit, instance, jobs_busy,
                 hook="", clock=time.time, trigger=call_hook, upstream=None, fetch_head=None):
        self.application = application
        self.root = Path(root)
        self.file = self.root / ".deploy-guard.json"
        self.secret, self.commit, self.instance = secret, commit, instance
        self.jobs_busy, self.hook = jobs_busy, hook
        self.clock, self.trigger = clock, trigger
        self.lock = threading.RLock()
        self.writes = self.background = 0
        # (repo slug, branch)。Render の Auto-Deploy を切った本番で、新しいコミットを自分で配置する
        self.upstream = upstream
        self.fetch_head = fetch_head or (lambda: remote_head(*self.upstream))
        self.watch_file = self.root / ".deploy-watch.json"
        self._pending = None  # (まだ配置していない最新コミット, 最初に見た時刻)
        self._hook_failures = 0
        if upstream:
            # この版が動き始めたことを残す。Render で古い版へ戻した時に、見張りが最新へ勝手に戻さないため
            self._remember("live", commit)
        with self.lock:
            state = self._read()
            if (state and state.get("phase") == "sealed"
                    and state.get("target") == commit
                    and state.get("source_instance") != instance):
                self.file.unlink()

    def _read(self):
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("phase") not in {"waiting", "sealed", "error"}:
                raise ValueError("Invalid deployment state")
            return data
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {"phase": "error", "reason": "unreadable_state"}

    def _save(self, state):
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, self.file)

    def intake_open(self):
        """Public availability only; never expose deployment credentials or controls."""
        with self.lock:
            return self.root.is_dir() and self._read() is None

    def _busy(self):
        if self.writes or self.background:
            return True
        try:
            return self.jobs_busy()
        except Exception:
            # Corrupt/unavailable job metadata must never be treated as idle.
            return True

    def background_task(self, function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            with self.lock:
                self.background += 1
            try:
                return function(*args, **kwargs)
            finally:
                with self.lock:
                    self.background -= 1
        return wrapped

    def drain(self, target):
        with self.lock:
            state = self._read()
            if state and state.get("reason") == "unreadable_state":
                return {"ready": False, "retry": False, "reason": "unreadable_state"}
            if not state or state.get("target") != target:
                state = {"target": target, "phase": "waiting", "attempts": 0,
                         "source_instance": self.instance, "requested_at": self.clock(),
                         "retry_after": self.clock() + 90}
            ready = not self._busy()
            if ready:
                state["phase"] = "sealed"
            self._save(state)
            return {"ready": ready, "retry": bool(self.hook), "phase": state["phase"]}

    def tick(self):
        with self.lock:
            state = self._read()
            if (not state or state["phase"] != "waiting" or not self.hook
                    or self.clock() < state["retry_after"] or self._busy()):
                return
            # Seal before requesting replacement; never admit a job between
            # the idle check and the platform accepting a deploy.
            state["phase"] = "sealed"
            state["attempts"] += 1
            self._save(state)
        try:
            self.trigger(self.hook)
            logging.info("Generation finished; queued Render deployment retried")
        except Exception:
            # Don't log exception text: network errors can contain hook keys.
            logging.error("Deployment retry failed; existing service remains protected")
            with self.lock:
                current = self._read()
                if current == state:
                    current["phase"] = "waiting" if state["attempts"] < 3 else "error"
                    current["retry_after"] = self.clock() + 120 * state["attempts"]
                    self._save(current)

    def _watch_state(self):
        try:
            data = json.loads(self.watch_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _remember(self, key, commit):
        try:
            data = self._watch_state()
            data[key] = [c for c in data.get(key, []) if c != commit][-19:] + [commit]
            tmp = self.watch_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.watch_file)
        except OSError:
            logging.error("Could not record the deployment history")

    def check_upstream(self):
        """Render の Auto-Deploy を切った本番で、main の新しいコミットを生成の合間に配置する。

        push のたびに Render が配置を始めると、生成中は Pre-deploy が「保留」で終わり、Render はそれを
        失敗と数えて「Deploy failed」のメールを送っていた（2026-09-27 に6件。中身は保留で、実害なし）。
        ここでは生成中でない時だけ Deploy Hook を呼ぶので、保留による失敗が出ない。
        - 新しいコミットを最初に見てから UPSTREAM_SETTLE_SECONDS 待つ（続けて push されたら最後の1回だけ）
        - 同じコミットを自動で呼ぶのは1回だけ。一度動いた版にも呼ばない（Render で古い版へ戻した時に最新へ戻さない）
        - 配置の途中（Pre-deploy が来た後）や人の確認待ちの間は何もしない。待つ間も受付は止めない
          （Hook を呼んでから Pre-deploy までの間に生成が始まれば、従来どおり保留して終了後に再試行する）
        返り値: 配置を呼んだコミット。呼ばなければ None
        """
        if not (self.hook and self.upstream):
            return None
        try:
            head = self.fetch_head()
        except Exception:
            logging.warning("Could not read the latest commit on GitHub; will retry")
            return None
        if not head or head == self.commit:
            self._pending = None
            return None
        known = self._watch_state()
        if head in known.get("requested", []) or head in known.get("live", []):
            return None
        now = self.clock()
        if not self._pending or self._pending[0] != head:
            self._pending = (head, now)
            return None
        if now - self._pending[1] < UPSTREAM_SETTLE_SECONDS:
            return None
        with self.lock:
            if self._read() is not None or self._busy():
                return None
        try:
            self.trigger(self.hook)
        except Exception:
            # Don't log exception text: network errors can contain hook keys.
            self._hook_failures += 1
            if self._hook_failures < 3:
                logging.error("Deployment request for a new commit failed; will retry")
                return None
            logging.error("Deployment request for a new commit failed 3 times; deploy it manually on Render")
            self._hook_failures = 0
            self._remember("requested", head)
            return None
        self._hook_failures = 0
        self._remember("requested", head)
        # アプリは logging の既定（WARNING 以上）のままなので、運用で見る行は標準出力へ出す（Render の Logs に残る）
        print(f"[deploy-watch] New commit {head[:7]} found on GitHub; deployment requested while no generation is running",
              flush=True)
        return head

    def watch(self):
        polls = 0
        while True:
            time.sleep(15)
            try:
                self.tick()
            except Exception:
                logging.error("Deployment state check failed; manual inspection required")
            polls += 1
            if polls % (UPSTREAM_POLL_SECONDS // 15) == 0:
                try:
                    self.check_upstream()
                except Exception:
                    logging.error("New-commit check failed; will retry")

    @staticmethod
    def response(start, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        start(code, [("Content-Type", "application/json; charset=utf-8"),
                     ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
                     ("Retry-After", "30")])
        return [body]

    def __call__(self, environ, start):
        method, path = environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO", "")
        if path == CONTROL_PATH:
            try:
                size = int(environ.get("CONTENT_LENGTH", "0"))
                stamp = environ.get("HTTP_X_DEPLOY_TIME", "")
                if method != "POST" or not 0 < size <= 512 or abs(self.clock() - int(stamp)) > 60:
                    raise ValueError()
                body = environ["wsgi.input"].read(size)
                actual = environ.get("HTTP_X_DEPLOY_SIGNATURE", "")
                if not hmac.compare_digest(signature(self.secret, stamp, body), actual):
                    raise ValueError()
                target = json.loads(body)["target"]
                if not isinstance(target, str) or not re.fullmatch(r"[0-9a-f]{40}", target):
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                return self.response(start, "403 Forbidden", {"error": "forbidden"})
            return self.response(start, "200 OK", self.drain(target))
        mutation = method not in {"GET", "HEAD", "OPTIONS"} and not (method == "POST" and path == "/login")
        if not mutation:
            return self.application(environ, start)
        with self.lock:
            if self._read() or not self.root.is_dir():
                return self.response(start, "503 Service Unavailable",
                                     {"ok": False, "code": "deployment_pending", "error": MESSAGE})
            self.writes += 1
        def respond():
            result = None
            try:
                result = self.application(environ, start)
                yield from result
            finally:
                try:
                    if hasattr(result, "close"):
                        result.close()
                finally:
                    with self.lock:
                        self.writes -= 1
        return respond()


def interrupt_orphaned_jobs(output_dir, clock=time.time):
    """起動した時点で「生成中」のまま残っている回を「中断」にする。戻り値は中断にした回のID。

    この本番は1プロセス（Gunicorn 1 worker）で、更新は生成が終わってから入れ替わる。起動時点で
    終わっていない回は、前のプロセスが落ちて（メモリ不足など）止まったもので、もう進まない。
    残すと jobs_busy が永久に「生成中」と答え、更新も新しい回も止まる（2026-09-27 18:27〜19:55 の実障害。
    ルノアールの回が running のまま残り、再開も新しい回も 503 になった）。中断にすれば画面から再開できる。
    """
    from datetime import datetime
    stamp = datetime.fromtimestamp(clock()).isoformat(timespec="seconds")
    changed = []
    for directory in sorted(Path(output_dir).iterdir()):
        path = directory / "job.json"
        if not directory.is_dir() or not path.exists():
            continue
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # 読めない回は jobs_busy が安全側（生成中）に倒す。人が確認する
        if not isinstance(state, dict) or state.get("status") in TERMINAL:
            continue
        state.update(status="interrupted", updated_at=stamp,
                     message="サーバーの再起動で中断しました。「再開」で続きから作れます。")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        changed.append(directory.name)
    return changed


def install(module, root):
    try:
        orphaned = interrupt_orphaned_jobs(module.OUTPUT_DIR)
        if orphaned:
            logging.warning("Marked jobs left running by a stopped process as interrupted: %s",
                            ", ".join(orphaned))
    except Exception:
        logging.error("Orphaned job check failed; jobs left running still block deployments")

    def jobs_busy():
        with module._jobs_lock:
            if any(state.get("status") not in TERMINAL for state in module._jobs.values()):
                return True
        for directory in module.OUTPUT_DIR.iterdir():
            if not directory.is_dir():
                continue
            path = directory / "job.json"
            if path.exists() and json.loads(path.read_text(encoding="utf-8")).get("status") not in TERMINAL:
                return True
        return False
    slug, branch = os.environ.get("RENDER_GIT_REPO_SLUG", ""), os.environ.get("RENDER_GIT_BRANCH", "")
    upstream = (slug, branch) if slug and branch else None
    guard = DeployGuard(module.app, root, os.environ["SECRET_KEY"],
                        os.environ.get("RENDER_GIT_COMMIT", "local"),
                        os.environ.get("RENDER_INSTANCE_ID", f"local-{os.getpid()}"),
                        jobs_busy, os.environ.get("RENDER_DEPLOY_HOOK", ""), upstream=upstream)
    if upstream and guard.hook:
        print(f"[deploy-watch] Watching {slug}@{branch} for new commits; deploys between generations", flush=True)
    module._run_pipeline_thread = guard.background_task(module._run_pipeline_thread)
    module._deploy_guard = guard
    threading.Thread(target=guard.watch, daemon=True, name="deployment-guard").start()
    return guard


def predeploy():
    """Fail this attempt quickly while busy; the running server retries on idle."""
    from urllib.parse import urlsplit
    url = os.environ["RENDER_EXTERNAL_URL"]
    parsed = urlsplit(url)
    # Render supplies this URL. Avoid forwarding a signed control message elsewhere.
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".onrender.com") or parsed.username or parsed.password:
        raise RuntimeError("RENDER_EXTERNAL_URL must be the Render HTTPS hostname")
    body = json.dumps({"target": os.environ["RENDER_GIT_COMMIT"]}).encode()
    stamp = str(int(time.time()))
    request = urllib.request.Request(url.rstrip("/") + CONTROL_PATH, data=body,
        headers={"Content-Type": "application/json", "X-Deploy-Time": stamp,
                 "X-Deploy-Signature": signature(os.environ["SECRET_KEY"], stamp, body)})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
            result = json.load(response)
    except Exception:
        raise SystemExit("Deployment guard unavailable; keeping existing instance running") from None
    if result.get("ready") is not True:
        reason = "automatic retry queued after generation" if result.get("retry") else "operator review required"
        raise SystemExit("Deployment postponed: " + reason)
    print("Generation drained; new work blocked until replacement is ready")


if __name__ == "__main__":
    predeploy()
