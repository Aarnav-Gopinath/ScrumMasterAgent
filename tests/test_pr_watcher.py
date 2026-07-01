"""Tests for the PR Review Watcher sub-agent (event + stale-PR sweep)."""

from __future__ import annotations

from datetime import timedelta

from agent.services.config import load_config
from agent.subagents import pr_watcher
from agent.subagents.pr_watcher import extract_issue_refs
from tests.conftest import NOW


def _config():
    return load_config("config.yml")


def test_extract_issue_refs_dedupes_and_orders():
    assert extract_issue_refs("Closes #4 and also #4", "see #12") == [4, 12]
    assert extract_issue_refs(None, "") == []


def test_pr_event_notes_referenced_issue(client):
    event = {
        "action": "opened",
        "pull_request": {
            "number": 101,
            "title": "Add login page styling",
            "body": "Closes #4 — adds the CSS.",
        },
    }
    summary = pr_watcher.run(client, _config(), event)

    assert summary == [(4, "noted-pr-101")]
    posted = client.repo.posted_comments
    assert len(posted) == 1
    assert posted[0]["issue_number"] == 4
    assert "PR #101" in posted[0]["body"]


def test_pr_event_with_no_reference_does_nothing(client):
    event = {"action": "opened", "pull_request": {"number": 55, "title": "misc", "body": ""}}
    assert pr_watcher.run(client, _config(), event) == []
    assert client.repo.posted_comments == []


def test_check_stale_prs_pings_old_open_pr(client):
    # Fixture PR #101 was opened 8 days ago; threshold defaults to staleness_days=2.
    summary = pr_watcher.check_stale_prs(client, _config(), NOW)

    actions = dict(summary)
    assert actions[101] == "pinged"
    assert any(c["issue_number"] == 101 for c in client.repo.posted_comments)


def test_check_stale_prs_skips_fresh_pr(client):
    # A generous threshold makes the 8-day-old PR "fresh".
    summary = pr_watcher.check_stale_prs(client, _config(), NOW, review_threshold_days=30)

    actions = dict(summary)
    assert actions[101].startswith("skip:fresh")
    assert client.repo.posted_comments == []
