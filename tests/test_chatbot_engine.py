"""Tests unitaires pour modules/chatbot_engine.py — état in-memory."""
import time

import pytest
import modules.chatbot_engine as ce


@pytest.fixture(autouse=True)
def clear_messages():
    """Vide le store de messages avant et après chaque test."""
    ce.clear_messages()
    ce._last_ping_ok = False
    yield
    ce.clear_messages()
    ce._last_ping_ok = False


# ── add_incoming_message / get_messages ───────────────────────────────────────

class TestStore:
    def test_get_messages_initially_empty(self):
        assert ce.get_messages() == []

    def test_add_incoming_appends_message(self):
        ce.add_incoming_message("Salut", "BotUser")
        msgs = ce.get_messages()
        assert len(msgs) == 1
        assert msgs[0]["role"] == "bot"
        assert msgs[0]["text"] == "Salut"

    def test_add_incoming_sets_timestamp(self):
        before = time.time()
        ce.add_incoming_message("test")
        after = time.time()
        ts = ce.get_messages()[0]["ts"]
        assert before <= ts <= after

    def test_get_messages_returns_copy(self):
        ce.add_incoming_message("msg")
        result = ce.get_messages()
        result.clear()
        assert len(ce.get_messages()) == 1

    def test_multiple_messages_ordered(self):
        ce.add_incoming_message("premier")
        ce.add_incoming_message("second")
        msgs = ce.get_messages()
        assert msgs[0]["text"] == "premier"
        assert msgs[1]["text"] == "second"

    def test_clear_messages_empties_store(self):
        ce.add_incoming_message("test")
        ce.clear_messages()
        assert ce.get_messages() == []


# ── get_connection_status() ───────────────────────────────────────────────────

class TestConnectionStatus:
    def test_false_before_any_send(self):
        assert ce.get_connection_status() is False


# ── fmt_time() ────────────────────────────────────────────────────────────────

class TestFmtTime:
    def test_returns_hhmmss_format(self):
        import datetime
        ts = datetime.datetime(2024, 6, 1, 14, 30, 45).timestamp()
        result = ce.fmt_time(ts)
        assert result == "14:30:45"

    def test_pads_single_digit_values(self):
        import datetime
        ts = datetime.datetime(2024, 6, 1, 8, 5, 3).timestamp()
        result = ce.fmt_time(ts)
        assert result == "08:05:03"
