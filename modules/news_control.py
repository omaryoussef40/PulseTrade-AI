"""
PulseTrade AI - News Engine Process Controls

Place this file in:
    modules/news_control.py

Purpose:
    Start, stop, and run the Benzinga news engine from the Streamlit dashboard
    without touching the trading engine.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = THIS_FILE.parents[1]
MODULES_DIR = THIS_FILE.parent
LOG_DIR = PROJECT_ROOT / "logs"
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

PID_FILE = DATA_DIR / "news_engine.pid"
PROCESS_LOG_FILE = LOG_DIR / "news_engine_process.log"
NEWS_ENGINE_FILE = MODULES_DIR / "news_engine.py"


@dataclass
class NewsProcessStatus:
    running: bool
    pid: int | None = None
    pid_file: str = str(PID_FILE)
    log_file: str = str(PROCESS_LOG_FILE)
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "pid": self.pid,
            "pid_file": self.pid_file,
            "log_file": self.log_file,
            "message": self.message,
        }


def _read_pid() -> int | None:
    try:
        if not PID_FILE.exists():
            return None
        raw = PID_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        return int(raw)
    except Exception:
        return None


def _write_pid(pid: int) -> None:
    PID_FILE.write_text(str(int(pid)), encoding="utf-8")


def _remove_pid() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def get_news_engine_status() -> NewsProcessStatus:
    pid = _read_pid()
    if pid is None:
        return NewsProcessStatus(running=False, message="No news engine PID file.")
    if _is_pid_running(pid):
        return NewsProcessStatus(running=True, pid=pid, message="News engine process is running.")
    _remove_pid()
    return NewsProcessStatus(running=False, message="Stale PID file removed.")


def start_news_engine() -> NewsProcessStatus:
    current = get_news_engine_status()
    if current.running:
        current.message = "News engine already running."
        return current

    if not NEWS_ENGINE_FILE.exists():
        return NewsProcessStatus(running=False, message=f"Missing file: {NEWS_ENGINE_FILE}")

    log_handle = PROCESS_LOG_FILE.open("a", encoding="utf-8")
    log_handle.write("\n--- Starting PulseTrade AI news engine ---\n")
    log_handle.flush()

    kwargs: dict[str, Any] = {
        "cwd": str(PROJECT_ROOT),
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }

    if os.name == "posix":
        kwargs["start_new_session"] = True
    elif os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

    process = subprocess.Popen(
        [sys.executable, str(NEWS_ENGINE_FILE)],
        **kwargs,
    )
    _write_pid(process.pid)
    time.sleep(0.5)

    if _is_pid_running(process.pid):
        return NewsProcessStatus(running=True, pid=process.pid, message="News engine started.")

    _remove_pid()
    return NewsProcessStatus(running=False, pid=process.pid, message="News engine failed to stay running. Check logs/news_engine_process.log.")


def stop_news_engine(timeout_seconds: float = 5.0) -> NewsProcessStatus:
    pid = _read_pid()
    if pid is None:
        return NewsProcessStatus(running=False, message="News engine is not running.")

    if not _is_pid_running(pid):
        _remove_pid()
        return NewsProcessStatus(running=False, pid=pid, message="News engine was not running. Stale PID removed.")

    try:
        if os.name == "posix":
            try:
                os.killpg(pid, signal.SIGTERM)
            except Exception:
                os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
    except Exception as exc:
        return NewsProcessStatus(running=True, pid=pid, message=f"Could not stop news engine: {exc}")

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not _is_pid_running(pid):
            _remove_pid()
            return NewsProcessStatus(running=False, pid=pid, message="News engine stopped.")
        time.sleep(0.2)

    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass
    _remove_pid()
    return NewsProcessStatus(running=False, pid=pid, message="News engine force-stopped.")


def run_news_poll_once(timeout_seconds: int = 60) -> dict[str, Any]:
    """Run one poll synchronously and return stdout/stderr for dashboard display."""
    if not NEWS_ENGINE_FILE.exists():
        return {"ok": False, "returncode": None, "stdout": "", "stderr": f"Missing file: {NEWS_ENGINE_FILE}"}

    try:
        completed = subprocess.run(
            [sys.executable, str(NEWS_ENGINE_FILE), "--once"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        with PROCESS_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write("\n--- Manual one-shot poll ---\n")
            f.write(completed.stdout or "")
            if completed.stderr:
                f.write("\nSTDERR:\n" + completed.stderr)
        return {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "returncode": None, "stdout": exc.stdout or "", "stderr": "Manual news poll timed out."}
    except Exception as exc:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": str(exc)}


def tail_news_process_log(max_chars: int = 5000) -> str:
    if not PROCESS_LOG_FILE.exists():
        return ""
    try:
        text = PROCESS_LOG_FILE.read_text(encoding="utf-8", errors="ignore")
        return text[-max_chars:]
    except Exception:
        return ""


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="PulseTrade AI News Engine Controls")
    parser.add_argument("action", choices=["status", "start", "stop", "once", "tail"])
    args = parser.parse_args()

    if args.action == "status":
        print(json.dumps(get_news_engine_status().as_dict(), indent=2))
    elif args.action == "start":
        print(json.dumps(start_news_engine().as_dict(), indent=2))
    elif args.action == "stop":
        print(json.dumps(stop_news_engine().as_dict(), indent=2))
    elif args.action == "once":
        print(json.dumps(run_news_poll_once(), indent=2))
    elif args.action == "tail":
        print(tail_news_process_log())
