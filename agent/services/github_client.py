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

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


def _branch_matches_issue_number(branch_name: str, issue_number: int) -> bool:
    # Match issue number as a numeric token to avoid #2 matching "feature/42-...".
    return re.search(rf"(?<!\d){issue_number}(?!\d)", branch_name or "") is not None


@dataclass
class CommitRef:
    """Normalized commit: just what status inference needs."""

    sha: str
    message: str
    date: Optional[datetime]
    author_login: Optional[str] = None


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

    def __init__(self, repo=None, github=None, org=None):
        """Store the injected repo. `repo` must expose PyGitHub's Repository read
        surface (get_issues, get_commits, get_pulls) plus per-issue get_comments /
        create_comment. No network happens here."""
        self.repo = repo
        self.github = github
        self.org = org

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

    @classmethod
    def from_org_token(cls, token: Optional[str], org_name: str) -> "GitHubClient":
        """Build a live org-scoped client from a GitHub token."""
        if not token:
            raise ValueError(
                "No GitHub token found. Set GITHUB_TOKEN in the environment "
                "(or pass token=...)."
            )
        from github import Auth, Github

        github = Github(auth=Auth.Token(token))
        org = github.get_organization(org_name)
        return cls(repo=None, github=github, org=org)

    # ----- reads -------------------------------------------------------------

    def _require_repo(self, repo=None):
        selected = repo or self.repo
        if selected is None:
            raise ValueError(
                "No repository provided. Pass `repo` explicitly or construct "
                "GitHubClient with a repo."
            )
        return selected

    @staticmethod
    def _is_pull_request(issue) -> bool:
        """PyGitHub returns PRs from get_issues() too; they have a non-None
        `pull_request` attribute. We always exclude them from story queries."""
        return getattr(issue, "pull_request", None) is not None

    def get_org_repos(
        self,
        exclude_repo: Optional[str] = None,
        max_repos: Optional[int] = None,
    ):
        """Return all non-archived repos in the org, excluding `exclude_repo` if set."""
        if self.org is None:
            raise ValueError("Org client not configured. Use GitHubClient.from_org_token(...).")

        repos = []
        for repo in self.org.get_repos(sort="pushed", direction="desc"):
            if getattr(repo, "archived", False):
                continue
            full_name = getattr(repo, "full_name", "")
            if exclude_repo and full_name.lower() == exclude_repo.lower():
                continue
            repos.append(repo)
            if max_repos is not None and len(repos) >= max_repos:
                logger.info(
                    "Limiting scan to first %d active repos (max_repos cap applied)",
                    max_repos,
                )
                break

        logger.info(
            "Found %d non-archived repos in org %s after exclusions.",
            len(repos),
            getattr(self.org, "login", "unknown"),
        )
        return repos

    def get_last_repo_activity(self, repo) -> Optional[datetime]:
        """Return the latest commit timestamp across all branches, if available."""
        source_repo = self._require_repo(repo)
        try:
            latest = next(iter(source_repo.get_commits()), None)
            if latest is None:
                return None
            author = getattr(latest.commit, "author", None)
            return getattr(author, "date", None) if author else None
        except Exception as exc:  # noqa: BLE001 - API/network failures should not crash loops.
            logger.warning(
                "Could not read last repo activity for %s: %s",
                getattr(source_repo, "full_name", getattr(source_repo, "name", "unknown-repo")),
                exc,
            )
            return None

    def get_issues(
        self,
        milestone_name: Optional[str] = None,
        state: str = "open",
        repo=None,
    ):
        """Return issues (PRs excluded) in `state`, optionally filtered to a milestone.

        Milestone filtering is done client-side by title so callers don't have to
        resolve a Milestone object first; it behaves identically against real GitHub.
        """
        source_repo = self._require_repo(repo)
        issues = [i for i in source_repo.get_issues(state=state) if not self._is_pull_request(i)]
        if milestone_name is not None:
            issues = [
                i
                for i in issues
                if getattr(i, "milestone", None) is not None
                and i.milestone.title == milestone_name
            ]
        return issues

    def get_open_issues(self, repo=None, milestone_name: Optional[str] = None):
        """Open issues in `repo` (PRs excluded). `milestone_name` is currently ignored."""
        source_repo = self._require_repo(repo)
        return [
            i for i in source_repo.get_issues(state="open") if not self._is_pull_request(i)
        ]

    def get_issue(self, repo_or_number, number: Optional[int] = None):
        """Return a single issue by number (raw PyGitHub-shaped object)."""
        if number is None:
            source_repo = self._require_repo()
            issue_number = repo_or_number
        else:
            source_repo = self._require_repo(repo_or_number)
            issue_number = number
        return source_repo.get_issue(issue_number)

    def get_comments(self, repo_or_number, number: Optional[int] = None):
        """Return the comment objects for an issue (each has `.created_at`)."""
        return list(self.get_issue(repo_or_number, number).get_comments())

    def search_commits(self, repo_or_ref, ref: Optional[str] = None) -> list[CommitRef]:
        """Commits whose message references `ref` (e.g. "#42").

        Handles commits with a None message. Returns normalized CommitRefs.
        """
        if ref is None:
            source_repo = self._require_repo()
            needle = repo_or_ref
        else:
            source_repo = self._require_repo(repo_or_ref)
            needle = ref

        results: list[CommitRef] = []
        for c in source_repo.get_commits():
            message = c.commit.message or ""
            if needle in message:
                # PyGitHub exposes author date at commit.commit.author.date.
                author = getattr(c.commit, "author", None)
                results.append(
                    CommitRef(
                        sha=c.sha,
                        message=message,
                        date=getattr(author, "date", None) if author else None,
                        author_login=getattr(getattr(c, "author", None), "login", None),
                    )
                )
        return results

    def search_issue_branch_commits(
        self,
        issue_number: int,
        repo=None,
    ) -> list[CommitRef]:
        """Commits reachable from branches whose name includes `issue_number`."""
        source_repo = self._require_repo(repo)
        results: list[CommitRef] = []
        for branch in source_repo.get_branches():
            branch_name = getattr(branch, "name", "") or ""
            if not _branch_matches_issue_number(branch_name, issue_number):
                continue
            for c in source_repo.get_commits(sha=branch_name):
                author = getattr(c.commit, "author", None)
                results.append(
                    CommitRef(
                        sha=c.sha,
                        message=c.commit.message or "",
                        date=getattr(author, "date", None) if author else None,
                        author_login=getattr(getattr(c, "author", None), "login", None),
                    )
                )
        return results

    def search_prs(self, repo_or_ref, ref: Optional[str] = None, state: str = "all") -> list[PullRef]:
        """Pull requests whose title or body references `ref` (e.g. "#42")."""
        if ref is None:
            source_repo = self._require_repo()
            needle = repo_or_ref
        else:
            source_repo = self._require_repo(repo_or_ref)
            needle = ref

        results: list[PullRef] = []
        for pr in source_repo.get_pulls(state=state):
            haystack = f"{pr.title or ''} {pr.body or ''}"
            if needle in haystack:
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

    def get_open_pulls(self, repo=None) -> list[PullRef]:
        """All open PRs, normalized (used by the PR staleness check)."""
        source_repo = self._require_repo(repo)
        return [
            PullRef(
                number=pr.number,
                title=pr.title or "",
                body=pr.body or "",
                created_at=getattr(pr, "created_at", None),
                state=pr.state,
            )
            for pr in source_repo.get_pulls(state="open")
        ]

    def get_branch_committers(self, repo, issue_number: int) -> list[str]:
        """Get unique GitHub logins that committed on issue-related message/branches."""
        source_repo = self._require_repo(repo)
        seen: list[str] = []

        # a) Message-reference commits first.
        for commit in self.search_commits(source_repo, f"#{issue_number}"):
            login = commit.author_login
            if login and login not in seen:
                seen.append(login)

        # b) Branch-name commits second.
        for commit in self.search_issue_branch_commits(issue_number, source_repo):
            login = commit.author_login
            if login and login not in seen:
                seen.append(login)

        logger.info(
            "Found %d unique committer(s) for issue #%s in %s",
            len(seen),
            issue_number,
            getattr(source_repo, "full_name", getattr(source_repo, "name", "unknown-repo")),
        )
        return seen

    # ----- writes ------------------------------------------------------------

    def post_comment(self, repo_or_number, number_or_body, body: Optional[str] = None):
        """Post a comment on an issue and return the created comment object."""
        if body is None:
            source_repo = self._require_repo()
            issue_number = repo_or_number
            comment_body = number_or_body
        else:
            source_repo = self._require_repo(repo_or_number)
            issue_number = number_or_body
            comment_body = body
        return self.get_issue(source_repo, issue_number).create_comment(comment_body)
