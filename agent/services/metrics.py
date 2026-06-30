"""Activity detection and status inference — the agent's brain.

`infer_status` is a pure function (no GitHub calls) so it's trivially unit-testable;
`now` is passed in explicitly rather than read from the clock so tests are
deterministic. `build_activity_snapshot` is the only function here that talks to the
client.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from agent.models import ActivitySnapshot, Story, StoryStatus
from agent.services.github_client import GitHubClient
from agent.services.notifier import AGENT_COMMENT_MARKER


def business_days_between(start: datetime, end: datetime) -> int:
    """Count weekdays (Mon–Fri) in the half-open interval (start_date, end_date].

    Used to measure the staleness gap so a story isn't flagged stale just because a
    weekend passed. Returns 0 if `end` is not after `start`.
    """
    if end <= start:
        return 0
    days = 0
    cursor = start.date() + timedelta(days=1)
    end_date = end.date()
    while cursor <= end_date:
        if cursor.weekday() < 5:  # 0=Mon ... 4=Fri
            days += 1
        cursor += timedelta(days=1)
    return days


def _max_date(dates: list[datetime | None]) -> datetime | None:
    present = [d for d in dates if d is not None]
    return max(present) if present else None


def _is_agent_comment(comment) -> bool:
    """True if `comment` was posted by this agent (carries the hidden marker).

    The agent's own reminders must not count as developer activity — otherwise a stale
    story would look "active" the moment we ping it, and we'd never flag it again.
    """
    return AGENT_COMMENT_MARKER in (getattr(comment, "body", "") or "")


def build_activity_snapshot(client: GitHubClient, story: Story) -> ActivitySnapshot:
    """Tally commits, PRs, and comments linked to `story`.

    GitHub has no native commit-to-issue API, so activity is inferred from "#<number>"
    references in commit messages and PR title/body, plus the issue's own comments.

    NOTE (scale): this issues several reads per story. For a large repo, cache the
    commit/PR scans across stories (or use a GraphQL batch query) — see github_client.
    """
    ref = f"#{story.number}"

    commits = client.search_commits(ref)
    prs = client.search_prs(ref)
    # Exclude the agent's own comments so a staleness reminder isn't mistaken for
    # developer activity on the next run.
    comments = [c for c in client.get_comments(story.number) if not _is_agent_comment(c)]

    return ActivitySnapshot(
        last_commit_at=_max_date([c.date for c in commits]),
        last_pr_at=_max_date([p.created_at for p in prs]),
        last_comment_at=_max_date([getattr(c, "created_at", None) for c in comments]),
        commit_count=len(commits),
        pr_count=len(prs),
        comment_count=len(comments),
    )


def infer_status(
    story: Story,
    snapshot: ActivitySnapshot,
    staleness_days: int,
    business_days_only: bool,
    now: datetime,
) -> StoryStatus:
    """Map a story + its activity to a StoryStatus. Pure function.

    Rules (in order):
      - closed issue                         -> DONE
      - open + has linked PR                 -> IN_REVIEW
      - open + no assignee                   -> NOT_STARTED
      - open + assigned + recent activity    -> IN_PROGRESS
      - open + assigned + stale/no activity  -> STALLED
    """
    if story.is_closed:
        return StoryStatus.DONE

    if snapshot.pr_count > 0:
        return StoryStatus.IN_REVIEW

    if not story.has_assignee:
        return StoryStatus.NOT_STARTED

    last = snapshot.last_activity_at
    if last is None:
        return StoryStatus.STALLED

    if business_days_only:
        gap = business_days_between(last, now)
    else:
        gap = (now - last).days

    return StoryStatus.IN_PROGRESS if gap <= staleness_days else StoryStatus.STALLED
