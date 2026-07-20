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

from agent.models import Story, StoryStatus
from agent.services.config import Config
from agent.services.github_client import GitHubClient
from agent.services.metrics import build_activity_snapshot, infer_status, is_repo_abandoned
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

    try:
        repos = client.get_org_repos(exclude_repo=config.agent_repo)
    except ValueError:
        repos = [client.repo] if client.repo is not None else []

    for repo in repos:
        last_activity = client.get_last_repo_activity(repo)
        repo_name = getattr(repo, "full_name", getattr(repo, "name", "unknown-repo"))
        if is_repo_abandoned(last_activity, now, config.abandoned_days):
            if last_activity is None:
                logger.info(
                    "Skipping %s — no activity in over %d days",
                    repo_name,
                    config.abandoned_days,
                )
            else:
                days_inactive = (now - last_activity).days
                logger.info("Skipping %s — no activity in %d days", repo_name, days_inactive)
            continue

        for issue in client.get_open_issues(repo):
            story = Story.from_issue(issue)
            snapshot = build_activity_snapshot(client, story, repo=repo)
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

    if state_path is not None:
        save_state(state_path, state)

    return summary
