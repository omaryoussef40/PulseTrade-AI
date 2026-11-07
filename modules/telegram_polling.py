"""One local Telegram receiver at a time, including callback execution/offsets."""
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import time

LOCK_DIR = Path(tempfile.gettempdir()) / "pulsetrade-telegram-polling"


class PollingSlot:
    def __init__(self, handle):
        self.handle = handle
        handle.seek(0)
        try:
            self.state = json.load(handle)
        except (ValueError, UnicodeError):
            self.state = {}
        if not isinstance(self.state, dict):
            self.state = {}

    def ready(self):
        return time.time() >= self.state.get("retry_at", 0)

    def save(self):
        self.handle.seek(0)
        json.dump(self.state, self.handle)
        self.handle.truncate()
        self.handle.flush()

    def failed(self, retry_after=0):
        now = time.time()
        failures = min(int(self.state.get("failures", 0)) + 1, 4)
        delay = max(min(60 * 2 ** (failures - 1), 300), float(retry_after or 0))
        warn = "last_warning" not in self.state or now - self.state["last_warning"] >= 300
        self.state.update(failures=failures, retry_at=now + delay)
        if warn:
            self.state["last_warning"] = now
        self.save()
        return warn, delay

    def succeeded(self):
        recovered = bool(self.state.get("failures"))
        if recovered:
            self.state.pop("failures", None)
            self.state.pop("retry_at", None)
            self.save()
        return recovered


@contextmanager
def polling_slot(bot_token):
    """Kernel lock is released on process exit; no stale lock-file cleanup needed.

    Shared across threads, processes and checkouts on this machine. Only a hash
    of the token is used in filenames; tokens are never persisted here.
    """
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = LOCK_DIR / (sha256(bot_token.encode()).hexdigest() + ".lock")
    path.touch(exist_ok=True)
    with path.open("r+", encoding="utf-8") as handle:
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(" ")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield None
                return
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield None
                return
        try:
            yield PollingSlot(handle)
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
