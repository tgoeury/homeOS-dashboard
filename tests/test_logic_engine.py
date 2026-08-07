"""Tests unitaires pour modules/logic_engine.py — moteur logique pur."""
import pytest
import modules.logic_engine as le
from modules.logic_engine import LogicMode


@pytest.fixture(autouse=True)
def reset_mode():
    """Restaure le mode FORWARD après chaque test."""
    yield
    le.set_mode(LogicMode.FORWARD)


# ── get_mode / set_mode ───────────────────────────────────────────────────────

class TestMode:
    def test_default_mode_is_forward(self):
        assert le.get_mode() == LogicMode.FORWARD

    def test_set_mode_changes_mode(self):
        le.set_mode(LogicMode.ML)
        assert le.get_mode() == LogicMode.ML

    def test_set_mode_back_to_forward(self):
        le.set_mode(LogicMode.CLAUDE)
        le.set_mode(LogicMode.FORWARD)
        assert le.get_mode() == LogicMode.FORWARD


# ── process_message() ─────────────────────────────────────────────────────────

class TestProcessMessage:
    def test_forward_returns_input_unchanged(self):
        assert le.process_message("bonjour") == "bonjour"

    def test_forward_handles_empty_string(self):
        assert le.process_message("") == ""

    def test_forward_handles_unicode(self):
        msg = "température 21°C — ça va?"
        assert le.process_message(msg) == msg

    def test_ml_mode_raises_not_implemented(self):
        le.set_mode(LogicMode.ML)
        with pytest.raises(NotImplementedError):
            le.process_message("test")

    def test_claude_mode_raises_not_implemented(self):
        le.set_mode(LogicMode.CLAUDE)
        with pytest.raises(NotImplementedError):
            le.process_message("test")


# ── generate_reply() ──────────────────────────────────────────────────────────

class TestGenerateReply:
    def test_forward_returns_ack_string(self):
        reply = le.generate_reply("hello")
        assert reply is not None
        assert "hello" in reply

    def test_forward_reply_is_string(self):
        assert isinstance(le.generate_reply("test"), str)

    def test_ml_mode_returns_none(self):
        le.set_mode(LogicMode.ML)
        assert le.generate_reply("test") is None

    def test_claude_mode_returns_none(self):
        le.set_mode(LogicMode.CLAUDE)
        assert le.generate_reply("test") is None


# ── is_operational() ──────────────────────────────────────────────────────────

class TestIsOperational:
    def test_always_true(self):
        assert le.is_operational() is True

    def test_still_true_in_ml_mode(self):
        le.set_mode(LogicMode.ML)
        assert le.is_operational() is True
