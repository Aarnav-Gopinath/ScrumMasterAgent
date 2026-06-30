"""Unit tests for state/memory. No network access; `now` is controlled."""

from __future__ import annotations

import json
from datetime import timedelta

from agent.services.state import (
    load_state,
    record_reminder,
    save_state,
    should_remind,
)
from tests.conftest import NOW

STALENESS_DAYS = 2


def test_should_remind_true_when_never_reminded():
    assert should_remind({}, 42, STALENESS_DAYS, NOW) is True


def test_no_double_remind_inside_window():
    state: dict = {}
    record_reminder(state, 42, NOW)
    # Same day → must not remind again.
    assert should_remind(state, 42, STALENESS_DAYS, NOW) is False
    # One day later, still inside the 2-day window → still no.
    assert should_remind(state, 42, STALENESS_DAYS, NOW + timedelta(days=1)) is False


def test_remind_again_after_window_passes():
    state: dict = {}
    record_reminder(state, 42, NOW)
    assert should_remind(state, 42, STALENESS_DAYS, NOW + timedelta(days=2)) is True
    assert should_remind(state, 42, STALENESS_DAYS, NOW + timedelta(days=5)) is True


def test_record_reminder_increments_count():
    state: dict = {}
    record_reminder(state, 7, NOW, last_status="stalled")
    record_reminder(state, 7, NOW + timedelta(days=3))
    assert state["7"]["reminder_count"] == 2
    assert state["7"]["last_status"] == "stalled"


def test_load_missing_file_returns_empty(tmp_path):
    assert load_state(str(tmp_path / "nope.json")) == {}


def test_save_then_load_roundtrips(tmp_path):
    path = str(tmp_path / "agent-state.json")
    state: dict = {}
    record_reminder(state, 1, NOW)
    save_state(path, state)
    assert load_state(path) == state
    # Sanity: it's valid JSON with the expected shape.
    with open(path) as fh:
        assert json.load(fh)["1"]["reminder_count"] == 1
