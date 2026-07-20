"""Unit tests for the pure-logic metrics functions. No network access."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent.models import Story, StoryStatus
from agent.services.metrics import (
    build_activity_snapshot,
    business_days_between,
    infer_status,
    is_repo_abandoned,
)
from tests.conftest import NOW, make_snapshot, make_story

STALENESS_DAYS = 2


# ----- infer_status: every branch -------------------------------------------


def test_closed_issue_is_done():
    story = make_story(state="closed")
    snapshot = make_snapshot(last_activity_days_ago=1)
    assert (
        infer_status(story, snapshot, STALENESS_DAYS, business_days_only=True, now=NOW)
        == StoryStatus.DONE
    )


def test_open_with_linked_pr_is_in_review():
    story = make_story(assignees=["alice"])
    snapshot = make_snapshot(prs_unique_count=1, last_activity_days_ago=1)
    assert (
        infer_status(story, snapshot, STALENESS_DAYS, business_days_only=True, now=NOW)
        == StoryStatus.IN_REVIEW
    )


def test_open_assigned_recent_is_in_progress():
    story = make_story(assignees=["alice"])
    snapshot = make_snapshot(last_activity_days_ago=1)  # within 2 business days
    assert (
        infer_status(story, snapshot, STALENESS_DAYS, business_days_only=True, now=NOW)
        == StoryStatus.IN_PROGRESS
    )


def test_open_assigned_stale_is_stalled():
    story = make_story(assignees=["alice"])
    snapshot = make_snapshot(last_activity_days_ago=5)  # > 2 business days
    assert (
        infer_status(story, snapshot, STALENESS_DAYS, business_days_only=True, now=NOW)
        == StoryStatus.STALLED
    )


def test_open_assigned_no_activity_is_stalled():
    story = make_story(assignees=["alice"])
    snapshot = make_snapshot(last_activity_days_ago=None)
    assert (
        infer_status(story, snapshot, STALENESS_DAYS, business_days_only=True, now=NOW)
        == StoryStatus.STALLED
    )


def test_open_unassigned_is_not_started():
    story = make_story(assignees=[])
    snapshot = make_snapshot(last_activity_days_ago=None)
    assert (
        infer_status(story, snapshot, STALENESS_DAYS, business_days_only=True, now=NOW)
        == StoryStatus.NOT_STARTED
    )


def test_calendar_days_mode_ignores_weekends():
    """With business_days_only=False, a 3-calendar-day gap exceeds staleness_days=2."""
    story = make_story(assignees=["alice"])
    snapshot = make_snapshot(last_activity_days_ago=3)
    assert (
        infer_status(story, snapshot, STALENESS_DAYS, business_days_only=False, now=NOW)
        == StoryStatus.STALLED
    )


# ----- business_days_between -------------------------------------------------


def test_business_days_skips_weekend():
    # Fri 2026-06-26 -> Mon 2026-06-29: only Monday counts.
    start = datetime(2026, 6, 26, 9, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 29, 9, 0, tzinfo=timezone.utc)
    assert business_days_between(start, end) == 1


def test_business_days_full_week():
    # Mon -> next Mon spans Tue,Wed,Thu,Fri,Mon = 5 weekdays.
    start = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 29, 9, 0, tzinfo=timezone.utc)
    assert business_days_between(start, end) == 5


def test_business_days_zero_when_end_not_after_start():
    t = datetime(2026, 6, 29, 9, 0, tzinfo=timezone.utc)
    assert business_days_between(t, t) == 0
    assert business_days_between(t, t - timedelta(days=1)) == 0


# ----- build_activity_snapshot against fixtures ------------------------------


def test_snapshot_links_commits_by_reference(client):
    story = Story.from_issue(client.get_issue(42))
    snapshot = build_activity_snapshot(client, story)
    # One SHA is discovered from both message and branch scans and must be deduplicated.
    # Commits: c3d4e5f (#42 add form validation), e5f6g7h (#42 fix edge case),
    #          d4e5f6g (branch-only: refine validation checks),
    #          fin042abc (#42 FIN-0042 add auth token validation) — added in session 4.
    assert snapshot.commits_unique_count == 4
    assert snapshot.prs_unique_count == 0
    assert snapshot.last_activity_at is not None


def test_snapshot_counts_multiple_message_references(client):
    story = Story.from_issue(client.get_issue(2))
    snapshot = build_activity_snapshot(client, story)
    assert snapshot.commits_unique_count == 2


def test_snapshot_links_pr_by_reference(client):
    story = Story.from_issue(client.get_issue(4))  # PR references #4
    snapshot = build_activity_snapshot(client, story)
    assert snapshot.prs_unique_count == 1
    assert snapshot.comments_unique_count == 1


def test_is_repo_abandoned_when_last_activity_missing(now):
    assert is_repo_abandoned(None, now, abandoned_days=30) is True


def test_is_repo_abandoned_when_gap_exceeds_threshold(now):
    assert is_repo_abandoned(now - timedelta(days=31), now, abandoned_days=30) is True


def test_is_repo_not_abandoned_inside_threshold(now):
    assert is_repo_abandoned(now - timedelta(days=29), now, abandoned_days=30) is False


def test_is_repo_not_abandoned_when_activity_is_today(now):
    assert is_repo_abandoned(now, now, abandoned_days=30) is False
