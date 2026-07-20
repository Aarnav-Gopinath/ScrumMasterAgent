"""Tests for the Standup Reporter sub-agent."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from agent.services.config import load_config
from agent.subagents import reporter
from tests.conftest import NOW


def _config():
    return load_config("config.yml")


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
    monkeypatch.setattr(
        "agent.subagents.reporter.is_repo_abandoned",
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
