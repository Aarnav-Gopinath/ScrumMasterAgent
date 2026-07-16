"""Tests for Jira client ticket extraction and fake lookup behavior."""

from __future__ import annotations

import json
import os

from agent.services.jira_client import FakeJiraClient


def _fixture_tickets() -> dict[str, dict]:
    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "jira_tickets.json")
    with open(fixture_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def test_get_ticket_returns_known_ticket():
    client = FakeJiraClient(_fixture_tickets())
    ticket = client.get_ticket("FIN-7811")
    assert ticket is not None
    assert ticket["status"] == "In Progress"


def test_get_ticket_returns_none_for_unknown():
    client = FakeJiraClient(_fixture_tickets())
    assert client.get_ticket("FIN-0000") is None


def test_get_tickets_for_issue_extracts_fin_references():
    client = FakeJiraClient(_fixture_tickets())
    messages = [
        "FIN-7811: Implement discrepancy scanner",
        "refactor cache layer",
        "FIN-6310 FIN-7811: tidy standup output",
    ]

    tickets = client.get_tickets_for_issue(messages)
    ids = [ticket["id"] for ticket in tickets]
    assert ids == ["FIN-7811", "FIN-6310"]


def test_get_tickets_for_issue_no_fin_reference_returns_empty():
    client = FakeJiraClient(_fixture_tickets())
    assert client.get_tickets_for_issue(["chore: update README", "fix typo"]) == []
