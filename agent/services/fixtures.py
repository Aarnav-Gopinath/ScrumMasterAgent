"""In-memory, fixture-backed stand-in for a PyGitHub Repository.

`FixtureRepo.load(path)` reads the JSON files under `path` and builds objects shaped
like PyGitHub's (issues with `.assignees[].login`, commits with `.commit.message`,
etc.) so `GitHubClient(FixtureRepo.load(...))` runs every code path with zero network.

Timestamps in the JSON are stored as *relative offsets* (`*_days_ago`) and resolved
against a base `now` at load time, so a "stalled" story stays stalled no matter when
you run the demo. Writes (`create_comment`) are captured on `repo.posted_comments`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional


def _resolve(days_ago: Optional[int], now: datetime) -> Optional[datetime]:
    """Turn a `*_days_ago` offset into an absolute timestamp (None stays None)."""
    if days_ago is None:
        return None
    return now - timedelta(days=days_ago)


# ----- PyGitHub-shaped value objects ----------------------------------------


class _User:
    def __init__(self, login: str):
        self.login = login


class _Label:
    def __init__(self, name: str):
        self.name = name


class _Milestone:
    def __init__(self, title: str):
        self.title = title


class _CommitDetail:
    """Mirrors PyGitHub's `commit.commit` sub-object."""

    def __init__(self, message: str, date: Optional[datetime]):
        self.message = message
        self.author = type("Author", (), {"date": date})()


class FixtureCommit:
    def __init__(
        self,
        sha: str,
        message: str,
        date: Optional[datetime],
        author_login: Optional[str] = None,
    ):
        self.sha = sha
        self.commit = _CommitDetail(message, date)
        self.author = _User(author_login) if author_login else None


class FixtureBranch:
    def __init__(self, name: str, commit_sha: Optional[str] = None):
        self.name = name
        # Minimal shape to match PyGitHub's branch.commit.sha
        self.commit = type("BranchCommit", (), {"sha": commit_sha})()


class FixturePull:
    def __init__(self, number, title, body, state, created_at):
        self.number = number
        self.title = title
        self.body = body
        self.state = state
        self.created_at = created_at


class FixtureComment:
    def __init__(self, body: str, created_at: Optional[datetime], comment_id: Optional[int] = None):
        self.body = body
        self.created_at = created_at
        self.id = comment_id


class FixtureIssue:
    def __init__(self, repo: "FixtureRepo", data: dict, now: datetime):
        self._repo = repo
        self.number = data["number"]
        self.title = data["title"]
        self.body = data.get("body", "")
        self.state = data.get("state", "open")
        self.created_at = _resolve(data.get("created_days_ago"), now)
        self.assignees = [_User(login) for login in data.get("assignees", [])]
        self.labels = [_Label(name) for name in data.get("labels", [])]
        ms = data.get("milestone")
        self.milestone = _Milestone(ms) if ms else None
        # PRs returned by get_issues() carry a non-None pull_request; plain issues None.
        self.pull_request = object() if data.get("is_pull_request") else None
        self._comments: list[FixtureComment] = []

    def get_comments(self):
        return list(self._comments)

    def create_comment(self, body: str) -> FixtureComment:
        comment = FixtureComment(body=body, created_at=datetime.now(timezone.utc))
        self._comments.append(comment)
        self._repo.posted_comments.append({"issue_number": self.number, "body": body})
        return comment


# ----- the repo --------------------------------------------------------------


class FixtureRepo:
    """Quacks like a PyGitHub Repository for the calls GitHubClient makes."""

    def __init__(self):
        self._issues: dict[int, FixtureIssue] = {}
        self._commits: list[FixtureCommit] = []
        self._commits_by_sha: dict[str, FixtureCommit] = {}
        self._branches: list[FixtureBranch] = []
        self._branch_commits: dict[str, list[FixtureCommit]] = {}
        self._pulls: list[FixturePull] = []
        self.posted_comments: list[dict] = []  # captured writes

    @classmethod
    def load(cls, path: str = "tests/fixtures", now: Optional[datetime] = None) -> "FixtureRepo":
        now = now or datetime.now(timezone.utc)
        repo = cls()

        issues = _read_json(os.path.join(path, "issues.json"))
        for data in issues:
            issue = FixtureIssue(repo, data, now)
            repo._issues[issue.number] = issue

        for data in _read_json(os.path.join(path, "commits.json")):
            commit = FixtureCommit(
                sha=data["sha"],
                message=data.get("message", ""),
                date=_resolve(data.get("date_days_ago"), now),
                author_login=data.get("author_login"),
            )
            repo._commits.append(commit)
            repo._commits_by_sha[commit.sha] = commit

        for data in _read_json(os.path.join(path, "branches.json")):
            branch_name = data["name"]
            commit_shas = data.get("commit_shas", [])
            repo._branches.append(
                FixtureBranch(
                    name=branch_name,
                    commit_sha=commit_shas[0] if commit_shas else None,
                )
            )
            repo._branch_commits[branch_name] = [
                repo._commits_by_sha[sha]
                for sha in commit_shas
                if sha in repo._commits_by_sha
            ]

        for data in _read_json(os.path.join(path, "prs.json")):
            repo._pulls.append(
                FixturePull(
                    number=data["number"],
                    title=data.get("title", ""),
                    body=data.get("body", ""),
                    state=data.get("state", "open"),
                    created_at=_resolve(data.get("created_days_ago"), now),
                )
            )

        for idx, data in enumerate(_read_json(os.path.join(path, "comments.json")), start=1):
            issue = repo._issues.get(data["issue_number"])
            if issue is not None:
                issue._comments.append(
                    FixtureComment(
                        body=data.get("body", ""),
                        created_at=_resolve(data.get("created_days_ago"), now),
                        comment_id=data.get("id", idx),
                    )
                )

        return repo

    # PyGitHub Repository surface used by GitHubClient -------------------------

    def get_issues(self, state: str = "open"):
        if state == "all":
            return list(self._issues.values())
        return [i for i in self._issues.values() if i.state == state]

    def get_issue(self, number: int) -> FixtureIssue:
        return self._issues[number]

    def get_commits(self, sha: Optional[str] = None):
        if sha is None:
            return list(self._commits)
        if sha in self._branch_commits:
            return list(self._branch_commits[sha])
        return []

    def get_branches(self):
        return list(self._branches)

    def get_pulls(self, state: str = "all"):
        if state == "all":
            return list(self._pulls)
        return [p for p in self._pulls if p.state == state]


def _read_json(file_path: str) -> list:
    if not os.path.exists(file_path):
        return []
    with open(file_path, "r", encoding="utf-8") as fh:
        return json.load(fh)
