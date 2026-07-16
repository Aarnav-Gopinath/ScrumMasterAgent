"""Posting side effects: comments, @mention reminders, and a Slack stub.

Wraps a GitHubClient so callers don't build comment bodies by hand. Every method
returns the created comment object (or None for the unimplemented Slack path) so the
caller can log the URL.
"""

from __future__ import annotations

import logging

from agent.models import Story
from agent.services.github_client import GitHubClient

logger = logging.getLogger(__name__)

# Hidden HTML marker stamped onto every comment the agent posts. GitHub renders HTML
# comments invisibly, so users never see it, but it lets the agent recognize its own
# comments — crucially so a staleness reminder isn't later counted as fresh "activity"
# (which would mask the very staleness it's flagging). See metrics.is_agent_comment.
AGENT_COMMENT_MARKER = "<!-- scrum-master-agent -->"


class Notifier:
    def __init__(self, client: GitHubClient):
        self.client = client

    def post_comment(self, issue_number: int, body: str, repo=None):
        """Post a comment on an issue and return the created comment object.

        The hidden AGENT_COMMENT_MARKER is appended so activity detection can later
        exclude the agent's own comments.
        """
        logger.info("Posting comment on #%s", issue_number)
        comment_body = f"{body}\n\n{AGENT_COMMENT_MARKER}"
        if repo is None:
            return self.client.post_comment(issue_number, comment_body)
        return self.client.post_comment(repo, issue_number, comment_body)

    def remind_assignee(self, story: Story, days_stale: int, repo=None):
        """Nudge a stale story.

        If it has assignees, @mention each by login with a friendly note. If it has no
        assignee, flag it as unassigned-and-stale instead (tagging no one)."""
        unit = "business day" if days_stale == 1 else "business days"
        if story.has_assignee:
            mentions = " ".join(f"@{login}" for login in story.assignees)
            body = (
                f"{mentions} 👋 Heads up — **#{story.number} {story.title}** hasn't had "
                f"any tracked activity in **{days_stale} {unit}**. Could you push an "
                f"update, link a PR, or move it forward? Thanks!"
            )
        else:
            body = (
                f"⚠️ **#{story.number} {story.title}** is stale (no activity in "
                f"{days_stale} {unit}) and has **no assignee**. It likely needs to be "
                f"picked up or triaged."
            )
        return self.post_comment(story.number, body, repo=repo)

    def ask_committers_for_status(
        self,
        repo,
        story: Story,
        committers: list[str],
        days_stale: int,
    ) -> None:
        """Ask known branch committers whether stale work is still active."""
        if not committers:
            self.remind_assignee(story, days_stale, repo=repo)
            return

        mentions = " ".join(f"@{login}" for login in committers)
        body = (
            f"Hey {mentions} — this issue hasn't had any activity in **{days_stale} days**. "
            "Could you let us know: is this still in progress, or is it ready to close? Thanks!"
        )
        self.post_comment(story.number, body, repo=repo)

    def post_to_slack(self, text: str):
        """Post to Slack — NOT IMPLEMENTED for the POC.

        TODO: POST {"text": text} to config.slack_webhook_url using the stdlib
        urllib/requests. Left as a stub so no HTTP dependency creeps into the POC.
        """
        logger.warning("post_to_slack is a stub; would have sent: %s", text)
        return None
