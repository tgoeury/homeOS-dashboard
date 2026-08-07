"""Tests unitaires pour modules/timer_service.py — minuteurs in-memory."""
import time
from unittest.mock import patch, MagicMock

import pytest
import modules.timer_service as ts


@pytest.fixture(autouse=True)
def clear_timers():
    """Remet _timers à zéro avant et après chaque test."""
    ts._timers.clear()
    yield
    ts._timers.clear()


# ── next_timer_name() ─────────────────────────────────────────────────────────

class TestNextTimerName:
    def test_returns_timer_1_when_empty(self):
        assert ts.next_timer_name() == "Timer 1"

    def test_avoids_conflict_with_existing(self):
        ts.start_timer("Timer 1", 60)
        assert ts.next_timer_name() == "Timer 2"

    def test_fills_gap_in_sequence(self):
        ts.start_timer("Timer 1", 60)
        ts.start_timer("Timer 3", 60)
        assert ts.next_timer_name() == "Timer 2"

    def test_continues_after_all_used(self):
        for i in range(1, 4):
            ts.start_timer(f"Timer {i}", 60)
        assert ts.next_timer_name() == "Timer 4"


# ── start_timer() / delete_timer() ───────────────────────────────────────────

class TestStartDelete:
    def test_start_returns_short_id(self):
        tid = ts.start_timer("Test", 120)
        assert isinstance(tid, str)
        assert len(tid) == 8

    def test_start_creates_timer_with_correct_fields(self):
        before = time.time()
        tid = ts.start_timer("Mon timer", 300)
        after = time.time()
        t = ts._timers[tid]
        assert t["name"] == "Mon timer"
        assert t["total_s"] == 300
        assert t["remaining_s"] == 300
        assert t["expired"] is False
        assert before <= t["started_at"] <= after

    def test_delete_removes_timer(self):
        tid = ts.start_timer("T", 60)
        ts.delete_timer(tid)
        assert tid not in ts._timers

    def test_delete_nonexistent_does_not_raise(self):
        ts.delete_timer("nonexistent_id")

    def test_multiple_timers_independent(self):
        t1 = ts.start_timer("A", 60)
        t2 = ts.start_timer("B", 120)
        assert t1 in ts._timers
        assert t2 in ts._timers
        assert ts._timers[t1]["name"] == "A"
        assert ts._timers[t2]["name"] == "B"


# ── tick_and_get() ────────────────────────────────────────────────────────────

class TestTickAndGet:
    def test_returns_empty_when_no_timers(self):
        assert ts.tick_and_get() == []

    def test_computes_remaining_from_started_at(self):
        tid = ts.start_timer("T", 100)
        ts._timers[tid]["started_at"] = time.time() - 30
        timers = ts.tick_and_get()
        assert len(timers) == 1
        assert timers[0]["remaining_s"] == pytest.approx(70, abs=2)

    def test_marks_expired_at_zero(self):
        tid = ts.start_timer("T", 10)
        ts._timers[tid]["started_at"] = time.time() - 20
        timers = ts.tick_and_get()
        assert timers[0]["expired"] is True
        assert timers[0]["remaining_s"] == 0

    def test_sorted_by_remaining_asc(self):
        t1 = ts.start_timer("Long", 1000)
        t2 = ts.start_timer("Short", 100)
        ts._timers[t1]["started_at"] = time.time() - 0
        ts._timers[t2]["started_at"] = time.time() - 50
        timers = ts.tick_and_get()
        assert timers[0]["remaining_s"] < timers[1]["remaining_s"]

    def test_does_not_tick_expired_timer(self):
        tid = ts.start_timer("T", 10)
        ts._timers[tid]["expired"] = True
        ts._timers[tid]["remaining_s"] = 0
        ts._timers[tid]["started_at"] = time.time() - 100
        timers = ts.tick_and_get()
        assert timers[0]["remaining_s"] == 0


# ── delete_expired() ──────────────────────────────────────────────────────────

class TestDeleteExpired:
    def test_removes_expired_timers(self):
        t1 = ts.start_timer("Done", 5)
        t2 = ts.start_timer("Active", 300)
        ts._timers[t1]["expired"] = True
        ts.delete_expired()
        assert t1 not in ts._timers
        assert t2 in ts._timers

    def test_no_op_when_no_expired(self):
        tid = ts.start_timer("T", 100)
        ts.delete_expired()
        assert tid in ts._timers

    def test_no_op_when_empty(self):
        ts.delete_expired()


# ── _make_beep() / ALARM_DATA_URI ─────────────────────────────────────────────

class TestAlarmDataUri:
    def test_alarm_is_data_uri(self):
        assert ts.ALARM_DATA_URI.startswith("data:audio/wav;base64,")

    def test_alarm_is_non_empty(self):
        assert len(ts.ALARM_DATA_URI) > 100
