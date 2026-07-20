"""Tests for detect_jira_discrepancies — pure logic, no network access."""

from __future__ import annotations

from datetime import timedelta

from agent.models import JiraDiscrepancy, StoryStatus, describe_discrepancy
from agent.services.jira_client import FakeJiraClient
from agent.services.metrics import detect_jira_discrepancies
from tests.conftest import NOW, make_snapshot, make_story

STALENESS_DAYS = 2


def _fake_jira(**tickets):
    """Build a FakeJiraClient from keyword args: ticket_id=status_str or dict."""
    data = {}
    for tid, val in tickets.items():
        if isinstance(val, str):
            data[tid] = {"id": tid, "summary": tid, "status": val, "assignee": None}
        else:
            data[tid] = val
    return FakeJiraClient(data)


# ── core detection cases ──────────────────────────────────────────────────────


def test_stalled_fin_in_progress_yields_jira_in_progress_no_commits():
    """STALLED story + Jira 'In Progress' → jira_in_progress_no_commits."""
    story = make_story(42, assignees=["bob"])
    snapshot = make_snapshot(
        last_activity_days_ago=6,
        commits_unique_count=1,
    )
    # Manually set commit_messages (make_snapshot doesn't set these)
    snapshot.commit_messages = ["#42 FIN-0042 add auth validation"]

    status = StoryStatus.STALLED
    jira = _fake_jira(**{"FIN-0042": "In Progress"})

    result = detect_jira_discrepancies(story, snapshot, status, jira, STALENESS_DAYS, NOW)

    assert len(result) == 1
    d = result[0]
    assert d.issue_number == 42
    assert d.ticket_id == "FIN-0042"
    assert d.ticket_status == "In Progress"
    assert d.discrepancy_type == "jira_in_progress_no_commits"
    assert d.days_since_activity > 0


def test_open_story_fin_done_yields_jira_done_issue_open():
    """Open story + Jira ticket 'Done' → jira_done_issue_open."""
    story = make_story(42, state="open", assignees=["bob"])
    snapshot = make_snapshot(last_activity_days_ago=1, commits_unique_count=1)
    snapshot.commit_messages = ["FIN-6310 finalize standup digest"]

    status = StoryStatus.IN_PROGRESS
    jira = _fake_jira(**{"FIN-6310": "Done"})

    result = detect_jira_discrepancies(story, snapshot, status, jira, STALENESS_DAYS, NOW)

    assert len(result) == 1
    d = result[0]
    assert d.ticket_id == "FIN-6310"
    assert d.discrepancy_type == "jira_done_issue_open"


def test_stalled_no_fin_references_returns_empty():
    """No FIN-\\d+ in commit messages produces no discrepancies."""
    story = make_story(42, assignees=["bob"])
    snapshot = make_snapshot(last_activity_days_ago=6, commits_unique_count=2)
    snapshot.commit_messages = ["#42 add form validation", "#42 fix edge case"]

    status = StoryStatus.STALLED
    jira = _fake_jira(**{"FIN-0042": "In Progress"})

    result = detect_jira_discrepancies(story, snapshot, status, jira, STALENESS_DAYS, NOW)

    assert result == []


def test_fin_ticket_not_found_with_commits_yields_no_ticket_found():
    """FIN ticket missing from Jira + story has commits → no_jira_ticket_found."""
    story = make_story(42, assignees=["bob"])
    snapshot = make_snapshot(last_activity_days_ago=2, commits_unique_count=1)
    snapshot.commit_messages = ["FIN-9999 fix something obscure"]

    status = StoryStatus.STALLED
    jira = FakeJiraClient({})  # FIN-9999 not in Jira

    result = detect_jira_discrepancies(story, snapshot, status, jira, STALENESS_DAYS, NOW)

    assert len(result) == 1
    assert result[0].discrepancy_type == "no_jira_ticket_found"
    assert result[0].ticket_id == "FIN-9999"


def test_fin_ticket_not_found_with_no_commits_returns_empty():
    """FIN ticket missing but commits_unique_count == 0 → no discrepancy reported."""
    story = make_story(42, assignees=["bob"])
    snapshot = make_snapshot(last_activity_days_ago=None, commits_unique_count=0)
    snapshot.commit_messages = ["FIN-9999 referenced in description only"]

    status = StoryStatus.STALLED
    jira = FakeJiraClient({})

    result = detect_jira_discrepancies(story, snapshot, status, jira, STALENESS_DAYS, NOW)

    assert result == []


def test_in_progress_fin_in_progress_returns_empty():
    """IN_PROGRESS story + Jira 'In Progress' → no discrepancy (happy path)."""
    story = make_story(42, assignees=["bob"])
    snapshot = make_snapshot(last_activity_days_ago=1, commits_unique_count=1)
    snapshot.commit_messages = ["FIN-0042 working on auth tokens"]

    status = StoryStatus.IN_PROGRESS
    jira = _fake_jira(**{"FIN-0042": "In Progress"})

    result = detect_jira_discrepancies(story, snapshot, status, jira, STALENESS_DAYS, NOW)

    assert result == []


def test_multiple_fin_tickets_one_discrepancy_each():
    """Two FIN tickets → one discrepancy each for mismatches."""
    story = make_story(42, state="open", assignees=["bob"])
    snapshot = make_snapshot(last_activity_days_ago=6, commits_unique_count=2)
    snapshot.commit_messages = [
        "#42 FIN-0042 add auth token validation",
        "FIN-6310 finalize standup digest",
    ]

    status = StoryStatus.STALLED
    jira = _fake_jira(**{"FIN-0042": "In Progress", "FIN-6310": "Done"})

    result = detect_jira_discrepancies(story, snapshot, status, jira, STALENESS_DAYS, NOW)

    types = {d.discrepancy_type for d in result}
    assert "jira_in_progress_no_commits" in types   # FIN-0042 stalled but In Progress
    assert "jira_done_issue_open" in types           # FIN-6310 Done but issue open
    assert len(result) == 2


# ── describe_discrepancy ──────────────────────────────────────────────────────


def test_describe_jira_in_progress_no_commits():
    d = JiraDiscrepancy(
        issue_number=42,
        ticket_id="FIN-0042",
        ticket_status="In Progress",
        github_status="stalled",
        days_since_activity=4,
        discrepancy_type="jira_in_progress_no_commits",
    )
    text = describe_discrepancy(d)
    assert "FIN-0042" in text
    assert "In Progress" in text
    assert "4 days" in text


def test_describe_jira_done_issue_open():
    d = JiraDiscrepancy(
        issue_number=245,
        ticket_id="FIN-6310",
        ticket_status="Done",
        github_status="in_progress",
        days_since_activity=0,
        discrepancy_type="jira_done_issue_open",
    )
    text = describe_discrepancy(d)
    assert "FIN-6310" in text
    assert "Done" in text
    assert "#245" in text


def test_describe_no_jira_ticket_found():
    d = JiraDiscrepancy(
        issue_number=42,
        ticket_id="FIN-9999",
        ticket_status="",
        github_status="stalled",
        days_since_activity=0,
        discrepancy_type="no_jira_ticket_found",
    )
    text = describe_discrepancy(d)
    assert "FIN-9999" in text
    assert "No Jira ticket found" in text
