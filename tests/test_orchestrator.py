"""Tests for orchestrator environment validation."""

from __future__ import annotations

import pytest

from agent import orchestrator


def _patch_common(monkeypatch):
    monkeypatch.setattr("agent.orchestrator.load_config", lambda path="config.yml": object())
    monkeypatch.setattr(
        "agent.orchestrator.GitHubClient.from_org_token",
        lambda token, org_name: object(),
    )


def test_missing_agent_mode_exits(monkeypatch):
    monkeypatch.delenv("AGENT_MODE", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    with pytest.raises(SystemExit) as exc:
        orchestrator.main()
    assert exc.value.code == 1


def test_invalid_agent_mode_exits(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "invalid")
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    with pytest.raises(SystemExit) as exc:
        orchestrator.main()
    assert exc.value.code == 1


def test_missing_github_token_exits(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "staleness")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        orchestrator.main()
    assert exc.value.code == 1
