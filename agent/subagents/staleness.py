"""Staleness Monitor sub-agent (cron, daily).

Finds STALLED open stories and posts an @mention reminder — but
only if `should_remind` says we haven't already pinged within the staleness window, so
running it twice in a day never double-reminds. State is mutated in place; persisting
it is the caller's job (pass `state_path` to have run() save at the end).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from agent.models import StoryStatus
from agent.services.config import Config
from agent.services.github_client import GitHubClient
from agent.services.notifier import AGENT_COMMENT_MARKER, Notifier
from agent.services.state import record_reminder, save_state, should_remind

logger = logging.getLogger(__name__)


def run(
    client: GitHubClient,
    config: Config,
    state: dict,
    now: datetime,
    notifier: Optional[Notifier] = None,
    state_path: Optional[str] = None,
    jira_client=None,
) -> list[tuple[int, str]]:
    """Remind on stale stories. Returns a list of (issue_number, action_taken)."""
    notifier = notifier or Notifier(client)
    summary: list[tuple[int, str]] = []

    # Scanning (activity/issues/status/discrepancies) runs in parallel across repos;
    # notifications and state writes below stay sequential — see scan_all_repos.
    results = client.scan_all_repos(
        config, now, state=state, jira_client=jira_client, max_repos=None, max_issues=30
    )

    for result in results:
        repo_name = result["repo"]
        if result.get("skipped"):
            reason = result.get("reason")
            if reason == "abandoned":
                days = result.get("days")
                if days is None:
                    logger.info(
                        "Skipping %s — no activity in over %d days",
                        repo_name,
                        config.abandoned_days,
                    )
                else:
                    logger.info("Skipping %s — no activity in %d days", repo_name, days)
            else:
                logger.warning("Skipping %s — scan error: %s", repo_name, result.get("error"))
            continue

        repo = result["repo_obj"]
        for story, status, snapshot in result["stories"]:
            if status is not StoryStatus.STALLED:
                logger.info("#%s is %s — no reminder needed", story.number, status.value)
                summary.append((story.number, f"skip:{status.value}"))
                continue

            if not should_remind(state, story.number, config.staleness_days, now):
                logger.info("#%s stalled but reminded recently — skipping", story.number)
                summary.append((story.number, "skip:already-reminded"))
                continue

            committers = client.get_branch_committers(repo, story.number)
            if committers:
                logger.info("Reminding branch committers for issue #%s: %s", story.number, committers)
                notifier.ask_committers_for_status(repo, story, committers, config.staleness_days)
                action = "reminded:committers"
            else:
                logger.info(
                    "No branch committers found for issue #%s — falling back to assignee",
                    story.number,
                )
                notifier.remind_assignee(story, config.staleness_days, repo=repo)
                action = "reminded:assignee"

            record_reminder(state, story.number, now, last_status=status.value)
            logger.info("Reminded on #%s", story.number)
            summary.append((story.number, action))

            # Jira discrepancy check — runs only when a reminder is also due.
            if jira_client is not None:
                from agent.models import describe_discrepancy
                discrepancies = [
                    d for d in result["discrepancies"] if d.issue_number == story.number
                ]
                if discrepancies:
                    types = [d.discrepancy_type for d in discrepancies]
                    logger.info("Jira discrepancy found for #%s: %s", story.number, types)
                    lines = [
                        f"- {describe_discrepancy(d)}" for d in discrepancies
                    ]
                    discrepancy_body = (
                        "⚠️ **Jira Discrepancy Detected**\n\n"
                        + "\n".join(lines)
                    )
                    notifier.post_comment(story.number, discrepancy_body, repo=repo)
                else:
                    logger.debug("No Jira discrepancies for #%s", story.number)

    if state_path is not None:
        save_state(state_path, state)

    return summary
