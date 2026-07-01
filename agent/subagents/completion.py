"""Story Completion Checker sub-agent (event: issues closed).

When an issue is closed, "closed" doesn't necessarily mean "done" — people close
duplicates, rejects, and mistakes. This checker verifies the closed story actually met
the team's definition of done: it has a linked PR *and* every required completion label
from config. If not, it posts a comment flagging the gap so the closure gets a second
look.

Pure logic — no LLM tokens. `event_payload` is the parsed GITHUB_EVENT_PATH dict passed
in by the orchestrator.
"""

from __future__ import annotations

import logging
from typing import Optional

from agent.models import Story
from agent.services.config import Config
from agent.services.github_client import GitHubClient
from agent.services.notifier import Notifier

logger = logging.getLogger(__name__)


def run(
    client: GitHubClient,
    config: Config,
    event_payload: dict,
    notifier: Optional[Notifier] = None,
) -> tuple[int, str]:
    """Handle an issues 'closed' event. Returns (issue_number, action) for logging.

    Skips PRs (they arrive on this event too) and issues closed *not* as "closed" —
    e.g. reopened events share the schema.
    """
    notifier = notifier or Notifier(client)

    action = event_payload.get("action")
    issue_data = event_payload.get("issue", {})
    number = issue_data.get("number")

    if action != "closed" or number is None:
        logger.info("Not a close event (action=%s) — ignoring.", action)
        return (number or -1, "skip:not-a-close")

    # Re-read the issue from the client so we get authoritative labels (the event
    # payload's label list can lag) and can check for a linked PR.
    story = Story.from_issue(client.get_issue(number))

    missing_labels = [lab for lab in config.completion_labels if lab not in story.labels]
    linked_prs = client.search_prs(f"#{number}")
    has_linked_pr = len(linked_prs) > 0

    if not missing_labels and has_linked_pr:
        logger.info("#%s closed and meets completion criteria.", number)
        return (number, "ok:complete")

    problems = []
    if missing_labels:
        problems.append("missing required label(s): " + ", ".join(f"`{l}`" for l in missing_labels))
    if not has_linked_pr:
        problems.append("no linked pull request references it")

    body = (
        f"⚠️ **#{number} {story.title}** was closed but doesn't meet the definition of "
        f"done:\n\n"
        + "\n".join(f"- {p}" for p in problems)
        + "\n\nIf this was closed as a duplicate or won't-fix, ignore this. Otherwise, "
        "please add the missing label(s) / link the PR, or reopen it."
    )
    notifier.post_comment(number, body)
    logger.info("Flagged #%s: %s", number, "; ".join(problems))
    return (number, "flagged")
