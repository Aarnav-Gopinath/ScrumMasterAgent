"""Tests for the Standup Reporter sub-agent."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from agent.services.config import load_config
from agent.services.jira_client import FakeJiraClient
from agent.subagents import reporter
from tests.conftest import NOW


def _config():
    return load_config("config.yml")


def _jira_in_progress_client():
    """FakeJiraClient with FIN-0042 In Progress (matches fixture stalled issue #42)."""
    return FakeJiraClient({
        "FIN-0042": {
            "id": "FIN-0042",
            "summary": "Wire up auth API",
            "status": "In Progress",
            "assignee": "bob",
        }
    })


def test_standup_posts_to_configured_issue_with_repo_sections(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = _config()

    body = reporter.run(client, config, NOW)

    posted = client.repo.posted_comments
    assert len(posted) == 1
    assert posted[0]["issue_number"] == config.standup_issue_number
    assert "Daily Standup" in body
    assert "### unknown-repo" in body


def test_reporter_skips_abandoned_repo(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Abandonment is now checked inside GitHubClient.scan_repo (parallel scan),
    # not in reporter.py itself — patch it at the source.
    monkeypatch.setattr(
        "agent.services.metrics.is_repo_abandoned",
        lambda last_activity, now, abandoned_days: True,
    )

    body = reporter.run(client, _config(), NOW)

    assert "No active work detected across UST-PACE repos." in body
    assert len(client.repo.posted_comments) == 1


def test_reporter_calls_teams_notifier_per_active_repo(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = _config()
    config = replace(
        config,
        teams=[{"name": "Core", "webhook_url": "https://x", "repos": ["unknown-repo"]}],
    )

    calls = {"for_repo": [], "posted": []}

    class _StubTeamsNotifier:
        def __init__(self, repo_name: str):
            self.repo_name = repo_name

        def post_if_configured(self, text: str) -> None:
            calls["posted"].append((self.repo_name, text))

    def _for_repo(repo_full_name, cfg):
        _ = cfg
        calls["for_repo"].append(repo_full_name)
        return _StubTeamsNotifier(repo_full_name)

    monkeypatch.setattr("agent.subagents.reporter.TeamsNotifier.for_repo", _for_repo)

    reporter.run(client, config, NOW)

    assert calls["for_repo"] == ["unknown-repo"]
    assert len(calls["posted"]) == 1


def test_reporter_skips_teams_notifier_when_teams_empty(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = _config()
    config = replace(config, teams=[], teams_fallback_webhook="")

    called = {"for_repo": 0}

    def _for_repo(repo_full_name, cfg):
        _ = repo_full_name
        _ = cfg
        called["for_repo"] += 1
        return SimpleNamespace(post_if_configured=lambda text: None)

    monkeypatch.setattr("agent.subagents.reporter.TeamsNotifier.for_repo", _for_repo)

    reporter.run(client, config, NOW)

    assert called["for_repo"] == 0


def test_reporter_jira_discrepancy_section_appears_when_discrepancies_exist(
    client, monkeypatch
):
    """When FIN-0042 is In Progress but issue #42 is STALLED, the digest includes
    the '## Jira Discrepancies' section."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = _config()

    body = reporter.run(client, config, NOW, jira_client=_jira_in_progress_client())

    assert "## Jira Discrepancies" in body
    assert "FIN-0042" in body


def test_reporter_jira_discrepancy_section_absent_when_none_found(client, monkeypatch):
    """When all Jira ticket states align (no rule fires), the section is omitted."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = _config()

    # FIN-0042 is "To Do" — no discrepancy rule fires for this status.
    jira = FakeJiraClient({
        "FIN-0042": {
            "id": "FIN-0042",
            "summary": "Wire up auth API",
            "status": "To Do",   # neither "In Progress" nor "Done" → no mismatch
            "assignee": None,
        }
    })

    body = reporter.run(client, config, NOW, jira_client=jira)

    assert "## Jira Discrepancies" not in body


def test_reporter_runs_cleanly_when_jira_client_none(client, monkeypatch):
    """jira_client=None (default) must not raise and must omit the Jira section."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = _config()

    body = reporter.run(client, config, NOW)  # no jira_client

    assert "## Jira Discrepancies" not in body
    assert "Daily Standup" in body
