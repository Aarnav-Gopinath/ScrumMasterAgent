"""Standup Reporter sub-agent (cron, every morning).

Aggregates stories, infers each one's status, asks Claude for a natural-
language digest (via services.llm), and posts it to the configured standup issue.

This is the only sub-agent that spends LLM tokens — and it skips the call entirely when
nothing is active, so a quiet sprint costs nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from agent.models import Story, StoryStatus
from agent.services.config import Config
from agent.services.github_client import GitHubClient
from agent.services.jira_client import JiraClient
from agent.services.llm import generate_standup_summary
from agent.services.metrics import build_activity_snapshot, infer_status
from agent.services.notifier import Notifier

logger = logging.getLogger(__name__)

# Statuses that represent live work worth reporting on. If none of the sprint's stories
# are in one of these, we skip the LLM call and post a short "nothing in progress" note.
_ACTIVE_STATUSES = {
    StoryStatus.IN_PROGRESS,
    StoryStatus.IN_REVIEW,
    StoryStatus.STALLED,
}


def run(
    client: GitHubClient,
    config: Config,
    now: datetime,
    notifier: Optional[Notifier] = None,
    jira_client: Optional[JiraClient] = None,
) -> str:
    """Build and post the daily standup digest. Returns the posted body (for logging).

    Considers open *and* recently-closed issues so the digest
    can celebrate what just landed, not only what's in flight.
    """
    _ = jira_client  # reserved for Session 2 Jira discrepancy reporting
    notifier = notifier or Notifier(client)

    issues = client.get_issues(state="all")
    stories_with_status = []
    for issue in issues:
        story = Story.from_issue(issue)
        snapshot = build_activity_snapshot(client, story)
        status = infer_status(
            story, snapshot, config.staleness_days, config.business_days_only, now
        )
        stories_with_status.append((story, status, snapshot))

    heading = f"## Daily Standup — {now.date().isoformat()}"

    has_active = any(status in _ACTIVE_STATUSES for _, status, _ in stories_with_status)
    if not has_active:
        logger.info("No active stories — posting a short note, skipping the LLM call.")
        body = f"{heading}\n\nNothing in progress right now. Enjoy the calm. 🌤️"
    else:
        logger.info("Generating standup digest for %d stories.", len(stories_with_status))
        summary = generate_standup_summary(stories_with_status)
        body = f"{heading}\n\n{summary}"

    notifier.post_comment(config.standup_issue_number, body)
    logger.info("Posted standup to issue #%s.", config.standup_issue_number)
    return body
