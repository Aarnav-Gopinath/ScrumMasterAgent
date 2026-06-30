"""Shared test fixtures: a fixed `now`, factory helpers, and a fixture-backed client.

Tests must run with zero network access, so the GitHubClient is always built over a
FixtureRepo. `NOW` is fixed (a Tuesday) so business-day math is deterministic.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from agent.models import ActivitySnapshot, Story
from agent.services.fixtures import FixtureRepo
from agent.services.github_client import GitHubClient

# 2026-06-30 is a Tuesday — keeps weekday/weekend logic predictable.
NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def client() -> GitHubClient:
    """A GitHubClient over the JSON fixtures, with timestamps resolved against NOW."""
    return GitHubClient(FixtureRepo.load(FIXTURE_DIR, now=NOW))


# ----- factory helpers for pure-logic unit tests -----------------------------


def make_story(
    number: int = 1,
    *,
    assignees: list[str] | None = None,
    state: str = "open",
    labels: list[str] | None = None,
    milestone: str | None = "Sprint 1",
) -> Story:
    return Story(
        number=number,
        title=f"Story {number}",
        assignees=assignees if assignees is not None else ["alice"],
        labels=labels or [],
        state=state,
        created_at=NOW - timedelta(days=10),
        milestone=milestone,
    )


def make_snapshot(
    *,
    last_activity_days_ago: int | None = None,
    pr_count: int = 0,
    commit_count: int = 0,
    comment_count: int = 0,
) -> ActivitySnapshot:
    """Build a snapshot whose most-recent activity is `last_activity_days_ago` before
    NOW (None means no activity at all)."""
    stamp = None if last_activity_days_ago is None else NOW - timedelta(days=last_activity_days_ago)
    return ActivitySnapshot(
        last_commit_at=stamp,
        last_pr_at=stamp if pr_count else None,
        last_comment_at=None,
        commit_count=commit_count,
        pr_count=pr_count,
        comment_count=comment_count,
    )
