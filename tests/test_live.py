"""Live smoke tests (opt-in with `pytest -m live`)."""

from __future__ import annotations

import os

import pytest

from agent.services.config import load_config
from agent.services.github_client import GitHubClient
from agent.services.jira_client import JiraClient

LIVE_REPO_NAME = "UST-Pace/observex-agents"
LIVE_ISSUE_NUMBER = 319
LIVE_TICKET_ID = "FIN-7851"


@pytest.mark.live
def test_live_github_and_jira_smoke():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        pytest.skip("GITHUB_TOKEN is required for live tests")

    config = load_config("config.yml")
    client = GitHubClient.from_org_token(token=token, org_name=config.org_name)

    repos = client.get_org_repos(exclude_repo=config.agent_repo)
    assert isinstance(repos, list)
    assert repos
    assert all(getattr(repo, "full_name", "").lower() != config.agent_repo.lower() for repo in repos)

    first_repo_issues = client.get_open_issues(repos[0])
    assert isinstance(first_repo_issues, list)

    target_repo = next(
        (
            repo
            for repo in repos
            if getattr(repo, "full_name", "").lower() == LIVE_REPO_NAME.lower()
        ),
        None,
    )
    assert target_repo is not None, f"Target repo {LIVE_REPO_NAME} not found in org repo list"
    issue = client.get_issue(target_repo, LIVE_ISSUE_NUMBER)
    assert issue.title

    jira_client = JiraClient.from_env()
    ticket = jira_client.get_ticket(LIVE_TICKET_ID)
    assert isinstance(ticket, dict)
    assert ticket.get("status")
