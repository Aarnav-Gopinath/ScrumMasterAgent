"""Tests for the Standup Reporter sub-agent.

Runs fully offline: with no ANTHROPIC_API_KEY, llm.generate_standup_summary falls back
to a deterministic text digest, so we can assert on its content.
"""

from __future__ import annotations

from agent.services.config import load_config
from agent.subagents import reporter
from tests.conftest import NOW


def _config():
    return load_config("config.yml")


def test_standup_posts_to_configured_issue(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = _config()

    body = reporter.run(client, config, NOW)

    posted = client.repo.posted_comments
    assert len(posted) == 1
    assert posted[0]["issue_number"] == config.standup_issue_number
    assert "Daily Standup" in body
    # #3 is the stalled story — it must surface in the digest / needs-attention list.
    assert "#3" in body
    assert "Needs attention" in body


def test_standup_skips_llm_when_nothing_active(client, monkeypatch, now):
    """With every story either not-started, closed, or otherwise inactive, the reporter
    posts a short note instead of a digest. We simulate that by making infer_status
    always return NOT_STARTED."""
    from agent.models import StoryStatus

    monkeypatch.setattr(
        "agent.subagents.reporter.infer_status",
        lambda *a, **k: StoryStatus.NOT_STARTED,
    )
    called = {"llm": False}
    monkeypatch.setattr(
        "agent.subagents.reporter.generate_standup_summary",
        lambda *a, **k: called.__setitem__("llm", True) or "should not be used",
    )

    body = reporter.run(client, _config(), now)

    assert called["llm"] is False
    assert "Nothing in progress" in body
