"""Domain models for the agent.

These are plain dataclasses (stdlib only) — the agent's own vocabulary, decoupled
from PyGitHub's object shapes. `Story.from_issue()` is duck-typed: it reads the
attributes a PyGitHub Issue exposes (`.number`, `.assignees[].login`, ...), which the
in-memory FixtureIssue used in tests also exposes. That lets the same code path run
against real GitHub and against local fixtures with no branching.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class StoryStatus(Enum):
    """The lifecycle state the agent infers for a story."""

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    STALLED = "stalled"
    IN_REVIEW = "in_review"
    DONE = "done"


@dataclass
class Story:
    """A sprint story, mapped from a GitHub issue (pull requests excluded upstream)."""

    number: int
    title: str
    assignees: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    state: str = "open"  # "open" | "closed"
    created_at: Optional[datetime] = None
    milestone: Optional[str] = None

    @property
    def is_closed(self) -> bool:
        return self.state == "closed"

    @property
    def has_assignee(self) -> bool:
        return len(self.assignees) > 0

    @classmethod
    def from_issue(cls, issue) -> "Story":
        """Build a Story from a PyGitHub-shaped issue object.

        Duck-typed against PyGitHub's Issue: `issue.assignees` is a list of objects
        with `.login`, `issue.labels` a list with `.name`, and `issue.milestone` is
        either None or has `.title`.
        """
        milestone = getattr(issue, "milestone", None)
        return cls(
            number=issue.number,
            title=issue.title,
            assignees=[u.login for u in (issue.assignees or [])],
            labels=[lab.name for lab in (issue.labels or [])],
            state=issue.state,
            created_at=getattr(issue, "created_at", None),
            milestone=milestone.title if milestone is not None else None,
        )


@dataclass
class ActivitySnapshot:
    """A point-in-time tally of activity linked to a story.

    Timestamps may be None when no activity of that kind exists. `last_activity_at`
    returns the most recent non-None timestamp, or None if there's been no activity.
    """

    last_commit_at: Optional[datetime] = None
    last_pr_at: Optional[datetime] = None
    last_comment_at: Optional[datetime] = None
    commits_unique_count: int = 0
    prs_unique_count: int = 0
    comments_unique_count: int = 0

    # Backward-compatible aliases for existing callers/tests.
    @property
    def commit_count(self) -> int:
        return self.commits_unique_count

    @property
    def pr_count(self) -> int:
        return self.prs_unique_count

    @property
    def comment_count(self) -> int:
        return self.comments_unique_count

    @property
    def last_activity_at(self) -> Optional[datetime]:
        stamps = [
            t
            for t in (self.last_commit_at, self.last_pr_at, self.last_comment_at)
            if t is not None
        ]
        return max(stamps) if stamps else None
