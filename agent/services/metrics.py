"""Activity detection and status inference — the agent's brain.

`infer_status` is a pure function (no GitHub calls) so it's trivially unit-testable;
`now` is passed in explicitly rather than read from the clock so tests are
deterministic. `build_activity_snapshot` is the only function here that talks to the
client.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from agent.models import ActivitySnapshot, JiraDiscrepancy, Story, StoryStatus
from agent.services.github_client import GitHubClient
from agent.services.jira_client import extract_ticket_ids
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


def is_repo_abandoned(
    last_activity: datetime | None, now: datetime, abandoned_days: int = 30
) -> bool:
    """True if repo has no activity or last activity is older than abandoned_days."""
    if last_activity is None:
        return True
    return (now - last_activity).days > abandoned_days


def build_activity_snapshot(
    client: GitHubClient,
    story: Story,
    repo=None,
) -> ActivitySnapshot:
    """Tally commits, PRs, and comments linked to `story`.

    GitHub has no native commit-to-issue API, so activity is inferred from "#<number>"
    references in commit messages and PR title/body, plus the issue's own comments.

    NOTE (scale): this issues several reads per story. For a large repo, cache the
    commit/PR scans across stories (or use a GraphQL batch query) — see github_client.
    """
    ref = f"#{story.number}"

    message_commits = client.search_commits(repo, ref) if repo is not None else client.search_commits(ref)
    branch_commits = (
        client.search_issue_branch_commits(story.number, repo)
        if repo is not None
        else client.search_issue_branch_commits(story.number)
    )
    commits_by_sha = {commit.sha: commit for commit in [*message_commits, *branch_commits]}

    prs = client.search_prs(repo, ref) if repo is not None else client.search_prs(ref)
    prs_by_number = {pr.number: pr for pr in prs}
    # Exclude the agent's own comments so a staleness reminder isn't mistaken for
    # developer activity on the next run.
    comments = (
        client.get_comments(repo, story.number)
        if repo is not None
        else client.get_comments(story.number)
    )
    filtered_comments = [c for c in comments if not _is_agent_comment(c)]
    comments_by_id = {
        getattr(comment, "id", idx): comment
        for idx, comment in enumerate(filtered_comments)
    }

    return ActivitySnapshot(
        last_commit_at=_max_date([c.date for c in commits_by_sha.values()]),
        last_pr_at=_max_date([p.created_at for p in prs_by_number.values()]),
        last_comment_at=_max_date(
            [getattr(c, "created_at", None) for c in comments_by_id.values()]
        ),
        commits_unique_count=len(commits_by_sha),
        prs_unique_count=len(prs_by_number),
        comments_unique_count=len(comments_by_id),
        commit_messages=[c.message for c in commits_by_sha.values()],
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

    if snapshot.prs_unique_count > 0:
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


def detect_jira_discrepancies(
    story: Story,
    snapshot: ActivitySnapshot,
    status: StoryStatus,
    jira_client,
    staleness_days: int,
    now: datetime,
) -> list[JiraDiscrepancy]:
    """Check for Jira/GitHub state mismatches on a story. Pure function — no API calls.

    Requires snapshot.commit_messages to be populated (see build_activity_snapshot).
    Returns an empty list when there are no FIN references in the commit messages or
    when all ticket states align with the GitHub story state.

    `staleness_days` is accepted for interface consistency but is not used in the
    current discrepancy rules.
    """
    ticket_ids = extract_ticket_ids(snapshot.commit_messages)
    if not ticket_ids:
        return []

    discrepancies: list[JiraDiscrepancy] = []
    for ticket_id in ticket_ids:
        ticket = jira_client.get_ticket(ticket_id)

        if ticket is None:
            # Only flag missing tickets when the story has real commit activity.
            if snapshot.commits_unique_count > 0:
                discrepancies.append(
                    JiraDiscrepancy(
                        issue_number=story.number,
                        ticket_id=ticket_id,
                        ticket_status="",
                        github_status=status.value,
                        days_since_activity=0,
                        discrepancy_type="no_jira_ticket_found",
                    )
                )
            continue

        ticket_status = ticket.get("status", "")

        if ticket_status == "In Progress" and status is StoryStatus.STALLED:
            days = (
                business_days_between(snapshot.last_activity_at, now)
                if snapshot.last_activity_at is not None
                else 0
            )
            discrepancies.append(
                JiraDiscrepancy(
                    issue_number=story.number,
                    ticket_id=ticket_id,
                    ticket_status=ticket_status,
                    github_status=status.value,
                    days_since_activity=days,
                    discrepancy_type="jira_in_progress_no_commits",
                )
            )
        elif ticket_status == "Done" and story.state == "open":
            discrepancies.append(
                JiraDiscrepancy(
                    issue_number=story.number,
                    ticket_id=ticket_id,
                    ticket_status=ticket_status,
                    github_status=status.value,
                    days_since_activity=0,
                    discrepancy_type="jira_done_issue_open",
                )
            )

    return discrepancies
