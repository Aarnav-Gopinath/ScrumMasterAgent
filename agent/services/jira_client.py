"""Jira client and fixture-friendly fake implementation."""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

import requests
from requests.auth import HTTPBasicAuth
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

_JIRA_TICKET_REF = re.compile(r"\bFIN-\d+\b")


def extract_ticket_ids(commit_messages: list[str]) -> list[str]:
    """Extract unique FIN ticket IDs in first-seen order."""
    seen: list[str] = []
    for message in commit_messages:
        for ticket_id in _JIRA_TICKET_REF.findall(message or ""):
            if ticket_id not in seen:
                seen.append(ticket_id)
    return seen


class JiraClient:
    def __init__(self, session: requests.Session, base_url: str):
        self.session = session
        self.base_url = base_url.rstrip("/")

    @classmethod
    def from_env(cls) -> "JiraClient":
        base_url = os.environ.get("JIRA_BASE_URL")
        email = os.environ.get("JIRA_EMAIL")
        api_token = os.environ.get("JIRA_API_TOKEN")

        missing = [
            name
            for name, value in (
                ("JIRA_BASE_URL", base_url),
                ("JIRA_EMAIL", email),
                ("JIRA_API_TOKEN", api_token),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "Missing required Jira environment variable(s): " + ", ".join(missing)
            )

        session = requests.Session()
        session.auth = HTTPBasicAuth(email, api_token)
        session.headers.update({"Accept": "application/json"})
        return cls(session=session, base_url=base_url)

    def get_ticket(self, ticket_id: str) -> dict | None:
        url = f"{self.base_url}/rest/api/3/issue/{ticket_id}"
        params = {"fields": "status,summary,assignee"}
        try:
            response = self.session.get(url, params=params, timeout=15)
            if response.status_code == 404:
                return None
            response.raise_for_status()
        except RequestException as exc:
            raise RuntimeError(f"Failed to fetch Jira ticket {ticket_id}: {exc}") from exc

        payload = response.json()
        fields = payload.get("fields", {})
        assignee = fields.get("assignee") or {}
        return {
            "id": payload.get("key") or ticket_id,
            "summary": fields.get("summary", ""),
            "status": (fields.get("status") or {}).get("name", ""),
            "assignee": assignee.get("displayName"),
        }

    def get_tickets_for_issue(self, commit_messages: list[str]) -> list[dict]:
        tickets: list[dict] = []
        for ticket_id in extract_ticket_ids(commit_messages):
            try:
                ticket = self.get_ticket(ticket_id)
            except RuntimeError as exc:
                logger.warning("%s", exc)
                continue
            if ticket is not None:
                tickets.append(ticket)
        return tickets


class FakeJiraClient:
    def __init__(self, tickets: dict[str, dict]):
        self.tickets = tickets

    def get_ticket(self, ticket_id: str) -> dict | None:
        return self.tickets.get(ticket_id)

    def get_tickets_for_issue(self, commit_messages: list[str]) -> list[dict]:
        tickets: list[dict] = []
        for ticket_id in extract_ticket_ids(commit_messages):
            ticket = self.get_ticket(ticket_id)
            if ticket is not None:
                tickets.append(ticket)
        return tickets
