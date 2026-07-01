"""Tests for the Story Completion Checker sub-agent."""

from __future__ import annotations

from agent.services.config import load_config
from agent.subagents import completion


def _config():
    return load_config("config.yml")


def test_close_without_done_label_or_pr_is_flagged(client):
    # Fixture #6 is closed with labels ["story"] (no "status: done") and no linked PR.
    event = {"action": "closed", "issue": {"number": 6}}
    number, action = completion.run(client, _config(), event)

    assert (number, action) == (6, "flagged")
    posted = client.repo.posted_comments
    assert len(posted) == 1
    assert posted[0]["issue_number"] == 6
    body = posted[0]["body"]
    assert "status: done" in body        # names the missing label
    assert "linked pull request" in body  # flags the missing PR


def test_non_close_event_is_ignored(client):
    event = {"action": "reopened", "issue": {"number": 6}}
    number, action = completion.run(client, _config(), event)

    assert action == "skip:not-a-close"
    assert client.repo.posted_comments == []


def test_complete_close_is_not_flagged(client, monkeypatch):
    """An issue closed WITH all required labels AND a linked PR passes silently.

    #4 is labelled to satisfy completion and PR #101 references it; we flip #4 to
    'closed' in the fixture repo and add the done label for this scenario.
    """
    issue = client.repo.get_issue(4)
    issue.state = "closed"
    # Give it the required completion label.
    from agent.services.fixtures import _Label

    issue.labels.append(_Label("status: done"))

    event = {"action": "closed", "issue": {"number": 4}}
    number, action = completion.run(client, _config(), event)

    assert (number, action) == (4, "ok:complete")
    assert client.repo.posted_comments == []
