"""Tests for GitHubClient helper methods over FixtureRepo."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


def test_get_branch_committers_deduplicates_across_sources(client):
    committers = client.get_branch_committers(client.repo, 42)
    assert committers == ["alice", "dave", "bob"]


def test_get_branch_committers_empty_when_no_matches(client):
    committers = client.get_branch_committers(client.repo, 777)
    assert committers == []


# ── repo activity cache ───────────────────────────────────────────────────────


def test_get_last_repo_activity_returns_cached_value_on_second_call(client, monkeypatch):
    """Second call with a populated state dict must use the cache, not the API."""
    now = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
    state: dict = {}

    # First call — cache miss, populates state.
    result1 = client.get_last_repo_activity(client.repo, state=state, now=now)
    assert result1 is not None
    assert "repo_cache" in state

    # Poison the fixture so any API hit would return None (different value).
    original_commits = client.repo._commits[:]
    client.repo._commits = []

    # Second call — must return cached result, not the poisoned API result.
    result2 = client.get_last_repo_activity(client.repo, state=state, now=now)
    assert result2 == result1

    # Restore fixture state for other tests.
    client.repo._commits = original_commits


def test_get_last_repo_activity_no_state_always_fetches(client):
    """Without state/now, the function always fetches from the fixture."""
    result = client.get_last_repo_activity(client.repo)
    assert result is not None
