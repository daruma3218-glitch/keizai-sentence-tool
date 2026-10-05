"""Reconcile abandoned bulk jobs once in the serving worker; never regenerate."""
import os
import threading
import uuid

import material_store as store

_OWNER = None


def runtime_owner():
    global _OWNER
    pid = os.getpid()
    # Lazy, so Gunicorn preload does not give new workers the parent's identity.
    if _OWNER is None or _OWNER["pid"] != pid:
        _OWNER = {"instance": os.environ.get("RENDER_INSTANCE_ID", "local"), "pid": pid,
                  "nonce": uuid.uuid4().hex}
    return dict(_OWNER)


def owner_alive(owner):
    if not isinstance(owner, dict) or owner.get("instance") != os.environ.get("RENDER_INSTANCE_ID", "local"):
        return False
    pid = owner.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return True
    if pid == os.getpid():
        return owner.get("nonce") == runtime_owner()["nonce"]
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # Only invalid PID proves exit.
        try:
            code = ctypes.c_ulong()
            ok = kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
            return not ok or code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)  # POSIX existence check only; never used on Windows.
        return True
    except ProcessLookupError:
        return False
    except (OSError, OverflowError):
        return True


class WorkerRecovery:
    def __init__(self, module):
        self.module = module
        self.pid = None
        self.pending = set()
        self.lock = threading.Lock()

    def __call__(self):
        pid = os.getpid()
        with self.lock:
            if self.pid == pid and not self.pending:
                return
            module = self.module
            # Before requests can start/resume work, reconcile only saved jobs
            # that have no owner in this worker. The service uses one worker.
            with module._RESUME_LOCK, module._QUEUE_LOCK, module._jobs_lock:
                owned = set(module._jobs) | set(module._QUEUE) | {module._RUNNING["job_id"]}
                folders = ([module.OUTPUT_DIR / name for name in self.pending] if self.pid == pid else
                           module.OUTPUT_DIR.iterdir() if module.OUTPUT_DIR.is_dir() else [])
                deferred = set()
                for folder in folders:
                    if (not folder.is_dir() or folder.is_symlink() or not store.ID.fullmatch(folder.name)
                            or folder.name in owned or folder.name.startswith("scene_fix_")):
                        continue
                    path = folder / "job.json"
                    try:
                        state = store.read(path, {})
                        if not isinstance(state, dict) or state.get("status") not in {"running", "queued"}:
                            continue
                        if owner_alive(state.get("runtime_owner")):
                            deferred.add(folder.name)
                            continue
                        state.update(recovery_previous_status=state["status"], status="interrupted",
                                     recovery_reason="worker_restarted", updated_at=store.now(),
                                     message="サーバーの再起動で中断しました。保存済みの素材を残して再開できます。")
                        store.save(path, state)
                    except (OSError, ValueError):
                        # Unknown state is not proof that restarting is safe.
                        # Leave the original for the existing history error display.
                        continue
            self.pid = pid
            self.pending = deferred
