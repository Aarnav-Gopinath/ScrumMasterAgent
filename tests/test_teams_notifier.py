"""Tests for TeamsNotifier routing."""

from __future__ import annotations

from types import SimpleNamespace

from agent.services.teams_notifier import TeamsNotifier


def _config(teams, fallback=""):
    return SimpleNamespace(teams=teams, teams_fallback_webhook=fallback)


def test_for_repo_returns_team_notifier():
    config = _config(
        teams=[
            {
                "name": "Core",
                "webhook_url": "https://outlook.office.com/webhook/core",
                "repos": ["UST-PACE/observex-ui"],
            }
        ]
    )
    notifier = TeamsNotifier.for_repo("UST-PACE/observex-ui", config)
    assert notifier is not None
    assert notifier.webhook_url == "https://outlook.office.com/webhook/core"


def test_for_repo_returns_fallback_when_unassigned():
    config = _config(
        teams=[{"name": "Core", "webhook_url": "https://outlook/core", "repos": ["UST-PACE/a"]}],
        fallback="https://outlook/fallback",
    )
    notifier = TeamsNotifier.for_repo("UST-PACE/other", config)
    assert notifier is not None
    assert notifier.webhook_url == "https://outlook/fallback"


def test_for_repo_returns_none_without_fallback():
    config = _config(
        teams=[{"name": "Core", "webhook_url": "https://outlook/core", "repos": ["UST-PACE/a"]}],
        fallback="",
    )
    assert TeamsNotifier.for_repo("UST-PACE/other", config) is None


def test_for_repo_match_is_case_insensitive():
    config = _config(
        teams=[
            {
                "name": "Core",
                "webhook_url": "https://outlook.office.com/webhook/core",
                "repos": ["ust-pace/observex-ui"],
            }
        ]
    )
    notifier = TeamsNotifier.for_repo("UST-PACE/OBSERVEX-UI", config)
    assert notifier is not None
    assert notifier.webhook_url == "https://outlook.office.com/webhook/core"
