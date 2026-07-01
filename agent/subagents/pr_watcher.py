"""PR Review Watcher sub-agent (event: pull_request opened/ready_for_review).

Two entry points:
  - run(...) reacts to a single PR event: it figures out which issue the PR closes
    and drops a note on that issue so the team knows review is pending.
  - check_stale_prs(...) is a cron-style sweep: open PRs sitting past a review
    threshold get a nudge comment.

This is pure logic — no LLM tokens. The event payload is the already-parsed dict that
GitHub Actions writes to GITHUB_EVENT_PATH; the orchestrator loads that file and passes
the dict in (see orchestrator.load_event).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

from agent.services.config import Config
from agent.services.github_client import GitHubClient
from agent.services.notifier import Notifier

logger = logging.getLogger(__name__)

# Matches "#42" issue references in free text (PR title/body).
_ISSUE_REF = re.compile(r"#(\d+)")


def extract_issue_refs(*texts: Optional[str]) -> list[int]:
    """Return the issue numbers referenced (as "#<n>") across the given texts.

    Deduplicated, preserving first-seen order. Empty/None texts are ignored.
    """
    seen: list[int] = []
    for text in texts:
        for match in _ISSUE_REF.findall(text or ""):
            number = int(match)
            if number not in seen:
                seen.append(number)
    return seen


def run(
    client: GitHubClient,
    config: Config,
    event_payload: dict,
    notifier: Optional[Notifier] = None,
) -> list[tuple[int, str]]:
    """Handle a pull_request event. Returns (issue_number, action) pairs for logging.

    Extracts the PR number and the issue(s) it references from the payload, then posts
    a "PR open for review" note on each referenced issue.
    """
    notifier = notifier or Notifier(client)

    pr = event_payload.get("pull_request", {})
    pr_number = pr.get("number")
    refs = extract_issue_refs(pr.get("title"), pr.get("body"))

    if not refs:
        logger.info("PR #%s references no issue — nothing to do.", pr_number)
        return []

    summary: list[tuple[int, str]] = []
    for issue_number in refs:
        body = (
            f"🔍 PR #{pr_number} is open and references this story. "
            f"It's ready for review — reviewers, please take a look when you can."
        )
        notifier.post_comment(issue_number, body)
        logger.info("Noted PR #%s review on issue #%s.", pr_number, issue_number)
        summary.append((issue_number, f"noted-pr-{pr_number}"))
    return summary


def check_stale_prs(
    client: GitHubClient,
    config: Config,
    now: datetime,
    review_threshold_days: Optional[int] = None,
    notifier: Optional[Notifier] = None,
) -> list[tuple[int, str]]:
    """Nudge open PRs that have been waiting for review too long.

    A PR is "stale for review" if it's been open longer than `review_threshold_days`
    (defaults to config.staleness_days). Returns (pr_number, action) pairs.

    NOTE: the POC's normalized PullRef doesn't carry review state or reviewer logins, so
    we approximate "no review" with open-age and ping on the PR itself. A production
    build would query PR reviews (GET /pulls/{n}/reviews) and @mention requested
    reviewers directly.
    """
    notifier = notifier or Notifier(client)
    threshold = review_threshold_days or config.staleness_days

    summary: list[tuple[int, str]] = []
    for pr in client.get_open_pulls():
        if pr.created_at is None:
            continue
        age_days = (now - pr.created_at).days
        if age_days < threshold:
            summary.append((pr.number, f"skip:fresh({age_days}d)"))
            continue
        body = (
            f"⏰ This PR has been open for **{age_days} days** without landing. "
            f"Reviewers, could you give it a look? If it's blocked, a note here helps."
        )
        # PRs are addressable as issues for commenting.
        notifier.post_comment(pr.number, body)
        logger.info("Pinged stale PR #%s (%d days open).", pr.number, age_days)
        summary.append((pr.number, "pinged"))
    return summary
