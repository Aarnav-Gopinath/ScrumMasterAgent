"""Tests for the LLM layer's offline fallback path (no real API calls)."""

from __future__ import annotations

from datetime import timedelta

from agent.models import ActivitySnapshot, Story, StoryStatus
from agent.services.llm import generate_standup_summary
from tests.conftest import NOW


def _story(number, title, assignees):
    return Story(number=number, title=title, assignees=assignees, milestone="Sprint 1")


def test_fallback_used_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    data = [
        (_story(2, "Login form", ["alice"]), StoryStatus.IN_PROGRESS,
         ActivitySnapshot(last_commit_at=NOW - timedelta(days=1), commit_count=2)),
        (_story(3, "Auth API", ["bob"]), StoryStatus.STALLED, ActivitySnapshot()),
    ]

    summary = generate_standup_summary(data)

    assert "LLM unavailable" in summary          # marked as the fallback
    assert "Needs attention" in summary
    assert "#3 Auth API is stalled" in summary   # stalled story surfaced
    assert "#2 Login form" in summary


def test_fallback_reports_nothing_stalled(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    data = [
        (_story(2, "Login form", ["alice"]), StoryStatus.IN_PROGRESS, ActivitySnapshot()),
    ]
    summary = generate_standup_summary(data)

    assert "Nothing stalled" in summary
