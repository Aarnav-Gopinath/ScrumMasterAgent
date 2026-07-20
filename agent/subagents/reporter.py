"""Standup Reporter sub-agent (cron, every morning)."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from agent.models import ActivitySnapshot, Story, StoryStatus
from agent.services.config import Config
from agent.services.github_client import GitHubClient
from agent.services.llm import generate_standup_summary
from agent.services.metrics import build_activity_snapshot, infer_status, is_repo_abandoned
from agent.services.notifier import Notifier
from agent.services.teams_notifier import TeamsNotifier

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = {
    StoryStatus.IN_PROGRESS,
    StoryStatus.IN_REVIEW,
    StoryStatus.STALLED,
}


def _resolve_target_repo(client: GitHubClient, config: Config):
    if getattr(client, "github", None) is not None:
        return client.github.get_repo(config.agent_repo)
    return client.repo


def run(
    client: GitHubClient,
    config: Config,
    now: datetime,
    anthropic_client=None,
    notifier: Optional[Notifier] = None,
) -> str:
    """Build and post the daily multi-repo standup digest."""
    _ = anthropic_client  # Anthropic client injection reserved for a later session.

    target_repo = _resolve_target_repo(client, config)
    post_client = GitHubClient(target_repo)
    notifier = notifier or Notifier(post_client)

    try:
        repos = client.get_org_repos(exclude_repo=config.agent_repo, max_repos=75)
    except ValueError:
        # Fixture/offline mode: a single injected repo is all we have.
        repos = [client.repo] if client.repo is not None else []

    logger.info("Reporter scanning %d repo(s).", len(repos))

    repo_summaries: list[dict] = []
    for repo in repos:
        repo_name = getattr(repo, "full_name", getattr(repo, "name", "unknown-repo"))
        last_activity = client.get_last_repo_activity(repo)
        if is_repo_abandoned(last_activity, now, config.abandoned_days):
            if last_activity is None:
                logger.info(
                    "Skipping %s — no activity in over %d days",
                    repo_name,
                    config.abandoned_days,
                )
            else:
                logger.info(
                    "Skipping %s — no activity in %d days",
                    repo_name,
                    (now - last_activity).days,
                )
            continue

        stories: list[tuple[Story, StoryStatus, ActivitySnapshot]] = []
        for issue in client.get_open_issues(repo):
            story = Story.from_issue(issue)
            snapshot = build_activity_snapshot(client, story, repo=repo)
            status = infer_status(
                story, snapshot, config.staleness_days, config.business_days_only, now
            )
            if status in _ACTIVE_STATUSES:
                stories.append((story, status, snapshot))

        logger.info("%s active stories found in %s", len(stories), repo_name)
        if stories:
            repo_summaries.append({"repo": repo_name, "stories": stories})

    active_stories = [
        item for repo_summary in repo_summaries for item in repo_summary["stories"]
    ]
    heading = f"## Daily Standup — {now.date().isoformat()}"

    if not active_stories:
        body = f"{heading}\n\nNo active work detected across UST-PACE repos."
        notifier.post_comment(config.standup_issue_number, body, repo=target_repo)
        logger.info("No active stories found across scanned repos.")
        return body

    if os.environ.get("ANTHROPIC_API_KEY"):
        logger.info("Generating standup digest via Claude path.")
    else:
        logger.info("ANTHROPIC_API_KEY missing; using fallback digest path.")
    digest = generate_standup_summary(active_stories)

    per_repo_sections = []
    for repo_summary in repo_summaries:
        repo_name = repo_summary["repo"]
        stories = repo_summary["stories"]
        lines = [f"### {repo_name} ({len(stories)} stories)"]
        for story, status, snapshot in stories:
            lines.append(
                f"- #{story.number} {story.title} — {status.value} "
                f"(commits={snapshot.commits_unique_count}, "
                f"prs={snapshot.prs_unique_count}, "
                f"comments={snapshot.comments_unique_count})"
            )
        per_repo_sections.append("\n".join(lines))

    body = f"{heading}\n\n" + "\n\n".join(per_repo_sections) + f"\n\n{digest}"
    notifier.post_comment(config.standup_issue_number, body, repo=target_repo)
    logger.info("Posted standup to issue #%s.", config.standup_issue_number)

    for repo_summary in repo_summaries:
        repo_name = repo_summary["repo"]
        repo_digest_lines = [f"### {repo_name} ({len(repo_summary['stories'])} stories)"]
        for story, status, _ in repo_summary["stories"]:
            repo_digest_lines.append(f"- #{story.number} {story.title} — {status.value}")
        repo_digest = "\n".join(repo_digest_lines)

        if not config.teams and not config.teams_fallback_webhook:
            logger.info("Teams notification skipped for %s (teams config is empty).", repo_name)
            continue

        teams_notifier = TeamsNotifier.for_repo(repo_name, config)
        if teams_notifier is None:
            logger.info("Teams notification skipped for %s (no channel configured).", repo_name)
            continue
        teams_notifier.post_if_configured(repo_digest)
        logger.info("Teams notification attempted for %s.", repo_name)

    return body
