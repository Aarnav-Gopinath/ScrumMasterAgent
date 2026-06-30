"""All GitHub reads/writes go through GitHubClient.

The class is constructed with a *repo* object (PyGitHub's Repository). Production
builds one from a token via `GitHubClient.from_token(...)`; tests and the demo runner
inject a `FixtureRepo` (see services/fixtures.py) that exposes the same read surface
and captures writes in memory. Because the swap happens at the repo layer, there's a
single client class and no live GitHub connection is needed to exercise any code path.

Issues are returned as raw PyGitHub-shaped objects (consumed via Story.from_issue).
Commits and PRs are normalized into small CommitRef / PullRef records so the metrics
layer never has to know PyGitHub's nested commit shape.

NOTE (scale): commit-to-issue linking is inferred from "#<number>" references, not a
native API. search_commits scans repo commits per call — fine for a small sandbox, but
for a large repo this is where a cache or a GraphQL batch query would go.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class CommitRef:
    """Normalized commit: just what status inference needs."""

    sha: str
    message: str
    date: Optional[datetime]


@dataclass
class PullRef:
    """Normalized pull request reference."""

    number: int
    title: str
    body: str
    created_at: Optional[datetime]
    state: str


class GitHubClient:
    """Thin wrapper over a PyGitHub Repository (or a fixture stand-in)."""

    def __init__(self, repo):
        """Store the injected repo. `repo` must expose PyGitHub's Repository read
        surface (get_issues, get_commits, get_pulls) plus per-issue get_comments /
        create_comment. No network happens here."""
        self.repo = repo

    @classmethod
    def from_token(cls, repo_name: str, token: Optional[str] = None) -> "GitHubClient":
        """Build a live client from a GitHub token.

        Reads GITHUB_TOKEN from the environment if `token` is not passed. Raises a
        clear error when no token is available rather than failing deep inside PyGitHub.
        """
        token = token or os.environ.get("GITHUB_TOKEN")
        if not token:
            raise RuntimeError(
                "No GitHub token found. Set GITHUB_TOKEN in the environment "
                "(or pass token=...)."
            )
        # Imported lazily so tests / the demo never need PyGitHub installed at import time.
        from github import Github

        repo = Github(token).get_repo(repo_name)
        return cls(repo)

    # ----- reads -------------------------------------------------------------

    @staticmethod
    def _is_pull_request(issue) -> bool:
        """PyGitHub returns PRs from get_issues() too; they have a non-None
        `pull_request` attribute. We always exclude them from story queries."""
        return getattr(issue, "pull_request", None) is not None

    def get_issues(self, milestone_name: Optional[str] = None, state: str = "open"):
        """Return issues (PRs excluded) in `state`, optionally filtered to a milestone.

        Milestone filtering is done client-side by title so callers don't have to
        resolve a Milestone object first; it behaves identically against real GitHub.
        """
        issues = [i for i in self.repo.get_issues(state=state) if not self._is_pull_request(i)]
        if milestone_name is not None:
            issues = [
                i
                for i in issues
                if getattr(i, "milestone", None) is not None
                and i.milestone.title == milestone_name
            ]
        return issues

    def get_open_issues(self, milestone_name: Optional[str] = None):
        """Open issues attached to the milestone (or all open issues if None)."""
        return self.get_issues(milestone_name=milestone_name, state="open")

    def get_issue(self, number: int):
        """Return a single issue by number (raw PyGitHub-shaped object)."""
        return self.repo.get_issue(number)

    def get_comments(self, number: int):
        """Return the comment objects for an issue (each has `.created_at`)."""
        return list(self.get_issue(number).get_comments())

    def search_commits(self, ref: str) -> list[CommitRef]:
        """Commits whose message references `ref` (e.g. "#42").

        Handles commits with a None message. Returns normalized CommitRefs.
        """
        results: list[CommitRef] = []
        for c in self.repo.get_commits():
            message = c.commit.message or ""
            if ref in message:
                # PyGitHub exposes author date at commit.commit.author.date.
                author = getattr(c.commit, "author", None)
                results.append(
                    CommitRef(
                        sha=c.sha,
                        message=message,
                        date=getattr(author, "date", None) if author else None,
                    )
                )
        return results

    def search_prs(self, ref: str, state: str = "all") -> list[PullRef]:
        """Pull requests whose title or body references `ref` (e.g. "#42")."""
        results: list[PullRef] = []
        for pr in self.repo.get_pulls(state=state):
            haystack = f"{pr.title or ''} {pr.body or ''}"
            if ref in haystack:
                results.append(
                    PullRef(
                        number=pr.number,
                        title=pr.title or "",
                        body=pr.body or "",
                        created_at=getattr(pr, "created_at", None),
                        state=pr.state,
                    )
                )
        return results

    def get_open_pulls(self) -> list[PullRef]:
        """All open PRs, normalized (used by the PR staleness check)."""
        return [
            PullRef(
                number=pr.number,
                title=pr.title or "",
                body=pr.body or "",
                created_at=getattr(pr, "created_at", None),
                state=pr.state,
            )
            for pr in self.repo.get_pulls(state="open")
        ]

    # ----- writes ------------------------------------------------------------

    def post_comment(self, number: int, body: str):
        """Post a comment on an issue and return the created comment object."""
        return self.get_issue(number).create_comment(body)
