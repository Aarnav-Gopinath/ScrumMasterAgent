"""Unit tests for state/memory. No network access; `now` is controlled."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from agent.services.state import (
    cache_repo_activity,
    get_cached_repo_activity,
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


# ── repo activity cache ───────────────────────────────────────────────────────


_REPO = "UST-PACE/some-repo"
_ACTIVITY = datetime(2026, 6, 28, 10, 0, 0, tzinfo=timezone.utc)


def test_cache_miss_when_repo_not_in_state():
    state: dict = {}
    assert get_cached_repo_activity(state, _REPO) is None


def test_cache_hit_when_fresh():
    """Storing and immediately reading back should be a cache hit."""
    state: dict = {}
    now = datetime.now(timezone.utc)
    cache_repo_activity(state, _REPO, _ACTIVITY, now)

    result = get_cached_repo_activity(state, _REPO, cache_ttl_hours=6, now=now)
    assert result == _ACTIVITY


def test_cache_miss_when_cached_at_older_than_ttl():
    """An entry cached 7 hours ago (TTL=6h) must not be returned."""
    state: dict = {}
    stale_time = datetime.now(timezone.utc) - timedelta(hours=7)
    cache_repo_activity(state, _REPO, _ACTIVITY, stale_time)

    result = get_cached_repo_activity(state, _REPO, cache_ttl_hours=6)
    assert result is None


def test_cache_repo_activity_stores_and_retrieves():
    """cache_repo_activity writes; get_cached_repo_activity reads it back."""
    state: dict = {}
    now = datetime.now(timezone.utc)
    cache_repo_activity(state, _REPO, _ACTIVITY, now)

    # Raw structure is correct.
    entry = state["repo_cache"][_REPO]
    assert "last_activity" in entry
    assert "cached_at" in entry
    assert datetime.fromisoformat(entry["last_activity"]) == _ACTIVITY

    # High-level retrieval also works.
    assert get_cached_repo_activity(state, _REPO, now=now) == _ACTIVITY
