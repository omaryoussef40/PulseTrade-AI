from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
from threading import Event
from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest
import engine
from modules import telegram_polling as polling


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(polling, "LOCK_DIR", tmp_path / "polling")
    monkeypatch.setattr(engine, "TELEGRAM_APPROVAL_STATE_FILE", tmp_path / "offset.json")


def test_lock_is_shared_between_threads_and_released_after_failure():
    with polling.polling_slot("test-bot") as first:
        assert first is not None
        with ThreadPoolExecutor() as pool:
            def competing():
                with polling.polling_slot("test-bot") as second:
                    return second is None
            assert pool.submit(competing).result(timeout=5)
        # Independent bots can receive replies at the same time.
        with polling.polling_slot("different-bot") as independent:
            assert independent is not None
    with pytest.raises(RuntimeError):
        with polling.polling_slot("test-bot"):
            raise RuntimeError("receiver crashed")
    with polling.polling_slot("test-bot") as recovered:
        assert recovered is not None


def test_lock_excludes_another_process_without_waiting():
    script = """
import sys
from pathlib import Path
from modules import telegram_polling as p
p.LOCK_DIR=Path(sys.argv[1])
with p.polling_slot('test-bot') as slot:
    print('busy' if slot is None else 'owned')
"""
    with polling.polling_slot("test-bot"):
        result = subprocess.run([sys.executable, "-c", script, str(polling.LOCK_DIR)],
            cwd=Path(engine.__file__).parent, capture_output=True, text=True, timeout=5, check=True)
    assert result.stdout.strip() == "busy"


def test_simultaneous_engine_and_dashboard_calls_make_one_request():
    entered, release = Event(), Event()
    tg = NS(bot_token="test-bot", chat_id="chat")
    def receive(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return {"result": [{"update_id": 10}]}
    with patch.object(engine, "telegram_post", side_effect=receive) as post:
        with ThreadPoolExecutor() as pool:
            first = pool.submit(engine.process_telegram_order_callbacks, None, None, tg, False)
            try:
                assert entered.wait(timeout=5)
                assert engine.process_telegram_order_callbacks(None, None, tg, False) == 0
                post.assert_called_once()
            finally:
                release.set()
            assert first.result(timeout=5) == 0
    assert engine.read_json_file(engine.TELEGRAM_APPROVAL_STATE_FILE, {})["offset"] == 11


def test_conflicts_back_off_across_callers_without_losing_offset_and_recover():
    tg = NS(bot_token="test-bot", chat_id="chat")
    engine.write_json_file(engine.TELEGRAM_APPROVAL_STATE_FILE, {"offset": 123})
    conflict = engine.TelegramAPIError("getUpdates", {"error_code": 409, "description": "Conflict"})
    with (patch.object(polling.time, "time", return_value=1000) as clock,
          patch.object(engine, "telegram_post", side_effect=[conflict, conflict, {"result": []}]) as post,
          patch.object(engine, "app_log") as log):
        assert engine.process_telegram_order_callbacks(None, None, tg, False) == 0
        for _ in range(10):
            engine.process_telegram_order_callbacks(None, None, tg, False)
        assert post.call_count == log.call_count == 1
        assert engine.read_json_file(engine.TELEGRAM_APPROVAL_STATE_FILE, {})["offset"] == 123
        clock.return_value = 1060
        engine.process_telegram_order_callbacks(None, None, tg, False)
        assert post.call_count == 2 and log.call_count == 1
        clock.return_value = 1179
        engine.process_telegram_order_callbacks(None, None, tg, False)
        assert post.call_count == 2
        clock.return_value = 1180
        engine.process_telegram_order_callbacks(None, None, tg, False)
        assert post.call_count == 3
        assert "recovered" in log.call_args.args[0]
        assert post.call_args.args[2]["offset"] == 123


def test_server_retry_after_is_respected():
    with patch.object(polling.time, "time", return_value=1000):
        with polling.polling_slot("test-bot") as slot:
            assert slot.failed(retry_after=600) == (True, 600)
    with patch.object(polling.time, "time", return_value=1500):
        with polling.polling_slot("test-bot") as slot:
            assert not slot.ready()


def test_unexpected_errors_are_not_hidden():
    with patch.object(engine, "telegram_post", side_effect=ValueError("unexpected response")):
        with pytest.raises(ValueError, match="unexpected response"):
            engine.process_telegram_order_callbacks(None, None, NS(bot_token="test", chat_id="chat"), False)


def test_intermittent_success_does_not_reset_warning_rate_limit():
    with patch.object(polling.time, "time", return_value=1000) as clock:
        with polling.polling_slot("test-bot") as slot:
            assert slot.failed()[0]
            clock.return_value = 1060
            assert slot.succeeded()
            clock.return_value = 1061
            assert not slot.failed()[0]
            clock.return_value = 1300
            assert slot.failed()[0]
