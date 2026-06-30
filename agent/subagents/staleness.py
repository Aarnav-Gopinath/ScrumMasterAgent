"""Staleness Monitor sub-agent (cron, daily).

Finds STALLED stories in the sprint milestone and posts an @mention reminder — but
only if `should_remind` says we haven't already pinged within the staleness window, so
running it twice in a day never double-reminds. State is mutated in place; persisting
it is the caller's job (pass `state_path` to have run() save at the end).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from agent.models import Story, StoryStatus
from agent.services.config import Config
from agent.services.github_client import GitHubClient
from agent.services.metrics import build_activity_snapshot, infer_status
from agent.services.notifier import Notifier
from agent.services.state import record_reminder, save_state, should_remind

logger = logging.getLogger(__name__)


def run(
    client: GitHubClient,
    config: Config,
    state: dict,
    now: datetime,
    notifier: Optional[Notifier] = None,
    state_path: Optional[str] = None,
) -> list[tuple[int, str]]:
    """Remind on stale stories. Returns a list of (issue_number, action_taken)."""
    notifier = notifier or Notifier(client)
    summary: list[tuple[int, str]] = []

    for issue in client.get_open_issues(config.sprint_milestone):
        story = Story.from_issue(issue)
        snapshot = build_activity_snapshot(client, story)
        status = infer_status(
            story, snapshot, config.staleness_days, config.business_days_only, now
        )

        if status is not StoryStatus.STALLED:
            logger.info("#%s is %s — no reminder needed", story.number, status.value)
            summary.append((story.number, f"skip:{status.value}"))
            continue

        if not should_remind(state, story.number, config.staleness_days, now):
            logger.info("#%s stalled but reminded recently — skipping", story.number)
            summary.append((story.number, "skip:already-reminded"))
            continue

        notifier.remind_assignee(story, config.staleness_days)
        record_reminder(state, story.number, now, last_status=status.value)
        logger.info("Reminded on #%s", story.number)
        summary.append((story.number, "reminded"))

    if state_path is not None:
        save_state(state_path, state)

    return summary
