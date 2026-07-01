"""Offline demo runner: `python -m agent.demo <mode>`.

Wires a GitHubClient over the in-memory FixtureRepo (no network, no tokens) and runs
a sub-agent against the JSON fixtures, printing what it reads and what it *would* post
(captured on repo.posted_comments). This is the per-phase "see it work" checkpoint.

Modes are added phase by phase: issues, status, staleness, standup, pr_watcher,
completion.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from agent.models import Story
from agent.services.config import load_config
from agent.services.fixtures import FixtureRepo
from agent.services.github_client import GitHubClient
from agent.services.metrics import build_activity_snapshot, infer_status
from agent.services.state import load_state
from agent.subagents import completion, pr_watcher, reporter, staleness

FIXTURE_PATH = "tests/fixtures"
DEMO_STATE_PATH = ".demo-state.json"


def _load_event(name: str) -> dict:
    with open(os.path.join(FIXTURE_PATH, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _client(now: datetime) -> GitHubClient:
    return GitHubClient(FixtureRepo.load(FIXTURE_PATH, now=now))


def demo_issues(now: datetime) -> None:
    """List every fixture issue mapped to a Story (proves loader + injection)."""
    config = load_config()
    client = _client(now)
    print(f"Open issues in milestone '{config.sprint_milestone}':\n")
    for issue in client.get_open_issues(config.sprint_milestone):
        story = Story.from_issue(issue)
        who = ", ".join(story.assignees) if story.assignees else "(unassigned)"
        print(f"  #{story.number:<3} {story.title:<32} assignees: {who}")


def demo_status(now: datetime) -> None:
    """Classify every story in the sprint (closed included) — all 5 statuses fire."""
    config = load_config()
    client = _client(now)
    print(f"Story statuses in milestone '{config.sprint_milestone}':\n")
    for issue in client.get_issues(config.sprint_milestone, state="all"):
        story = Story.from_issue(issue)
        snapshot = build_activity_snapshot(client, story)
        status = infer_status(
            story, snapshot, config.staleness_days, config.business_days_only, now
        )
        gap = snapshot.last_activity_at
        gap_str = gap.date().isoformat() if gap else "no activity"
        print(
            f"  #{story.number:<3} {status.value:<12} {story.title:<32} "
            f"(last activity: {gap_str}, commits={snapshot.commit_count} "
            f"prs={snapshot.pr_count} comments={snapshot.comment_count})"
        )


def _print_captured(client: GitHubClient) -> None:
    posted = client.repo.posted_comments
    if not posted:
        print("\n(no comments posted)")
        return
    print(f"\nCaptured {len(posted)} posted comment(s):")
    for c in posted:
        print(f"  → on #{c['issue_number']}: {c['body']}")


def demo_staleness(now: datetime) -> None:
    """Run the Staleness Monitor. State persists in .demo-state.json, so a second run
    shows 'already-reminded' skips (idempotency)."""
    config = load_config()
    client = _client(now)
    state = load_state(DEMO_STATE_PATH)
    print(f"Loaded state with {len(state)} remembered issue(s).\n")

    summary = staleness.run(client, config, state, now, state_path=DEMO_STATE_PATH)
    for number, action in summary:
        print(f"  #{number}: {action}")
    _print_captured(client)
    print(f"\nState saved to {DEMO_STATE_PATH} — run again to see no double-reminders.")


def demo_standup(now: datetime) -> None:
    """Run the Standup Reporter. Without ANTHROPIC_API_KEY set, llm.py returns its
    deterministic fallback digest — so this works fully offline."""
    config = load_config()
    client = _client(now)
    body = reporter.run(client, config, now)
    print("Standup digest that would be posted to "
          f"issue #{config.standup_issue_number}:\n")
    print(body)


def demo_pr_watcher(now: datetime) -> None:
    """Feed the PR-opened event fixture through the PR watcher."""
    config = load_config()
    client = _client(now)
    event = _load_event("event_pr_opened.json")
    summary = pr_watcher.run(client, config, event)
    for number, action in summary:
        print(f"  issue #{number}: {action}")
    _print_captured(client)


def demo_completion(now: datetime) -> None:
    """Feed the issue-closed event fixture through the completion checker.

    Fixture #6 is closed without the `status: done` label and no linked PR, so it
    gets flagged."""
    config = load_config()
    client = _client(now)
    event = _load_event("event_issue_closed.json")
    number, action = completion.run(client, config, event)
    print(f"  issue #{number}: {action}")
    _print_captured(client)


MODES = {
    "issues": demo_issues,
    "status": demo_status,
    "staleness": demo_staleness,
    "standup": demo_standup,
    "pr_watcher": demo_pr_watcher,
    "completion": demo_completion,
}


def main(argv: list[str]) -> int:
    if len(argv) < 1 or argv[0] not in MODES:
        print(f"usage: python -m agent.demo <{'|'.join(MODES)}>")
        return 2
    now = datetime.now(timezone.utc)
    MODES[argv[0]](now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
