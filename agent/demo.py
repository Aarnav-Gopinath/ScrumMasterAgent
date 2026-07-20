"""Offline demo runner: `python -m agent.demo <mode>`.

Wires a GitHubClient over the in-memory FixtureRepo (no network, no tokens) and runs
a sub-agent against the JSON fixtures, printing what it reads and what it *would* post
(captured on repo.posted_comments). This is the per-phase "see it work" checkpoint.

Modes are added phase by phase: issues, status, staleness, standup, pr_watcher,
completion.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from agent.models import Story, StoryStatus
from agent.services.config import load_config
from agent.services.fixtures import FixtureRepo
from agent.services.github_client import GitHubClient
from agent.services.llm import generate_standup_summary
from agent.services.metrics import build_activity_snapshot, infer_status, is_repo_abandoned
from agent.services.notifier import AGENT_COMMENT_MARKER, Notifier
from agent.services.state import load_state
from agent.services.teams_notifier import TeamsNotifier
from agent.subagents import completion, pr_watcher, reporter, staleness

FIXTURE_PATH = "tests/fixtures"
DEMO_STATE_PATH = ".demo-state.json"
logger = logging.getLogger(__name__)


def _load_event(name: str) -> dict:
    with open(os.path.join(FIXTURE_PATH, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _client(now: datetime) -> GitHubClient:
    return GitHubClient(FixtureRepo.load(FIXTURE_PATH, now=now))


def demo_issues(now: datetime) -> None:
    """List every fixture issue mapped to a Story (proves loader + injection)."""
    client = _client(now)
    print("Open issues:\n")
    for issue in client.get_open_issues():
        story = Story.from_issue(issue)
        who = ", ".join(story.assignees) if story.assignees else "(unassigned)"
        print(f"  #{story.number:<3} {story.title:<32} assignees: {who}")


def demo_status(now: datetime) -> None:
    """Classify every story in the sprint (closed included) — all 5 statuses fire."""
    config = load_config()
    client = _client(now)
    repo = client.repo
    last_activity = client.get_last_repo_activity(repo)
    abandoned = is_repo_abandoned(last_activity, now, config.abandoned_days)
    activity_note = (
        "no commits found"
        if last_activity is None
        else f"last commit: {last_activity.date().isoformat()}"
    )
    print(
        "Fixture repo status:\n"
        f"  abandoned={abandoned} ({activity_note}, threshold={config.abandoned_days} days)\n"
    )

    print("Story statuses across all issues:\n")
    for issue in client.get_issues(state="all", repo=repo):
        story = Story.from_issue(issue)
        snapshot = build_activity_snapshot(client, story, repo=repo)
        status = infer_status(
            story, snapshot, config.staleness_days, config.business_days_only, now
        )
        gap = snapshot.last_activity_at
        gap_str = gap.date().isoformat() if gap else "no activity"
        stalled_committers = (
            client.get_branch_committers(repo, story.number)
            if status is StoryStatus.STALLED
            else []
        )
        print(
            f"  #{story.number:<3} {status.value:<12} {story.title:<32} "
            f"(last activity: {gap_str}, commits={snapshot.commits_unique_count} "
            f"prs={snapshot.prs_unique_count} comments={snapshot.comments_unique_count})"
        )
        if status is StoryStatus.STALLED:
            if stalled_committers:
                print(f"       committers to contact: {', '.join(stalled_committers)}")
            else:
                print("       committers to contact: (none, fallback to assignee)")


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


class _DryRunNotifier(Notifier):
    def __init__(self, client: GitHubClient, repo_full_name: str):
        super().__init__(client)
        self.repo_full_name = repo_full_name

    def post_comment(self, issue_number: int, body: str, repo=None):
        comment_body = f"{body}\n\n{AGENT_COMMENT_MARKER}"
        target_repo = (
            getattr(repo, "full_name", self.repo_full_name)
            if repo is not None
            else self.repo_full_name
        )
        print(
            f"DRY RUN — would post to issue #{issue_number} in {target_repo}:\n"
            f"{comment_body}\n"
        )
        return {
            "issue_number": issue_number,
            "body": comment_body,
            "repo": target_repo,
            "dry_run": True,
        }


def _run_live_mode(mode: str, now: datetime) -> int:
    load_dotenv()
    config = load_config()
    token = os.environ.get("GITHUB_TOKEN")
    org_client = GitHubClient.from_org_token(token=token, org_name=config.org_name)
    repo_cap = 75 if mode in {"staleness", "standup"} else None
    repos = org_client.get_org_repos(exclude_repo=config.agent_repo, max_repos=repo_cap)

    if not repos:
        print("No repos found after exclusions.")
        return 1

    if mode in {"staleness", "standup"}:
        print(
            "DEMO MODE: scanning first 75 repos by recent activity.\n"
            "Remove max_repos cap for full org scan."
        )

    print(
        f"Live dry-run mode for {len(repos)} repo(s) in org {config.org_name} "
        f"(excluding {config.agent_repo})."
    )
    if mode == "standup" and not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "DEMO MODE: using fallback digest\n"
            "(no ANTHROPIC_API_KEY). Add key for Claude output."
        )

    event_payload = {}
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        with open(event_path, "r", encoding="utf-8") as fh:
            event_payload = json.load(fh)

    all_active_stories: list[tuple[Story, StoryStatus, object]] = []
    standup_repo_sections: list[tuple[str, list[tuple[Story, StoryStatus, object]]]] = []

    for repo in repos:
        repo_name = getattr(repo, "full_name", getattr(repo, "name", "unknown-repo"))
        print(f"\n=== {repo_name} ===")
        repo_client = GitHubClient(repo)
        notifier = _DryRunNotifier(repo_client, repo_name)
        if mode == "staleness":
            last_activity = org_client.get_last_repo_activity(repo)
            if is_repo_abandoned(last_activity, now, config.abandoned_days):
                if last_activity is None:
                    print(
                        f"Skipping {repo_name} — no activity in over {config.abandoned_days} days"
                    )
                else:
                    print(f"Skipping {repo_name} — no activity in {(now - last_activity).days} days")
                continue

            summary: list[tuple[int, str]] = []
            for issue in repo_client.get_open_issues(repo):
                story = Story.from_issue(issue)
                snapshot = build_activity_snapshot(repo_client, story, repo=repo)
                status = infer_status(
                    story, snapshot, config.staleness_days, config.business_days_only, now
                )
                if status is not StoryStatus.STALLED:
                    summary.append((story.number, f"skip:{status.value}"))
                    continue

                committers = repo_client.get_branch_committers(repo, story.number)
                if committers:
                    notifier.ask_committers_for_status(
                        repo, story, committers, config.staleness_days
                    )
                    summary.append((story.number, "dry-run:committers"))
                    teams_notifier = TeamsNotifier.for_repo(repo_name, config)
                    if teams_notifier is None:
                        print(
                            f"Teams routing: {repo_name} → "
                            "no channel configured (add webhook to config.yml)"
                        )
                    else:
                        print(
                            f"Teams routing: {repo_name} → "
                            "channel configured"
                        )
                    print(
                        f"DRY RUN outreach targets for #{story.number}: "
                        f"{', '.join(committers)}"
                    )
                else:
                    notifier.remind_assignee(story, config.staleness_days, repo=repo)
                    summary.append((story.number, "dry-run:assignee"))
                    teams_notifier = TeamsNotifier.for_repo(repo_name, config)
                    if teams_notifier is None:
                        print(
                            f"Teams routing: {repo_name} → "
                            "no channel configured (add webhook to config.yml)"
                        )
                    else:
                        print(
                            f"Teams routing: {repo_name} → "
                            "channel configured"
                        )
                    print(
                        f"DRY RUN outreach targets for #{story.number}: "
                        "(none found, fallback to assignee)"
                    )
            print(f"summary: {summary}")
        elif mode == "standup":
            last_activity = org_client.get_last_repo_activity(repo)
            if is_repo_abandoned(last_activity, now, config.abandoned_days):
                if last_activity is None:
                    print(
                        f"Skipping {repo_name} — no activity in over {config.abandoned_days} days"
                    )
                else:
                    print(f"Skipping {repo_name} — no activity in {(now - last_activity).days} days")
                continue

            repo_active_stories: list[tuple[Story, StoryStatus, object]] = []
            for issue in repo_client.get_open_issues(repo):
                story = Story.from_issue(issue)
                snapshot = build_activity_snapshot(repo_client, story, repo=repo)
                status = infer_status(
                    story, snapshot, config.staleness_days, config.business_days_only, now
                )
                if status in {StoryStatus.IN_PROGRESS, StoryStatus.IN_REVIEW, StoryStatus.STALLED}:
                    repo_active_stories.append((story, status, snapshot))

            print(f"=== {repo_name} ({len(repo_active_stories)} active stories) ===")
            for story, status, _snapshot in repo_active_stories:
                print(f"  - #{story.number} {story.title} [{status.value}]")

            teams_notifier = TeamsNotifier.for_repo(repo_name, config)
            if teams_notifier is None:
                print(f"Teams routing: {repo_name} → no channel configured (teams list empty)")
            else:
                print(f"Teams routing: {repo_name} → channel configured")

            if repo_active_stories:
                all_active_stories.extend(repo_active_stories)
                standup_repo_sections.append((repo_name, repo_active_stories))
        elif mode == "pr_watcher":
            if event_payload:
                summary = pr_watcher.run(repo_client, config, event_payload, notifier=notifier)
            else:
                summary = pr_watcher.check_stale_prs(repo_client, config, now, notifier=notifier)
            print(f"summary: {summary}")
        elif mode == "completion":
            result = completion.run(repo_client, config, event_payload, notifier=notifier)
            print(f"result: {result}")
        else:
            logger.warning("Unknown live mode %s", mode)
            return 2

    if mode == "standup":
        heading = f"## Daily Standup — {now.date().isoformat()}"
        if not all_active_stories:
            print(f"\n{heading}\n\nNo active work detected across UST-PACE repos.")
            return 0

        digest = generate_standup_summary(all_active_stories)
        print(f"\n{heading}")
        for repo_name, repo_stories in standup_repo_sections:
            print(f"\n### {repo_name} ({len(repo_stories)} active stories)")
            for story, status, _snapshot in repo_stories:
                print(f"- #{story.number} {story.title} — {status.value}")
        print(f"\n{digest}")
    return 0


MODES = {
    "issues": demo_issues,
    "status": demo_status,
    "staleness": demo_staleness,
    "standup": demo_standup,
    "pr_watcher": demo_pr_watcher,
    "completion": demo_completion,
}


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "live":
        live_mode = argv[1]
        if live_mode not in {"staleness", "standup", "pr_watcher", "completion"}:
            print("usage: python -m agent.demo live <staleness|standup|pr_watcher|completion>")
            return 2
        now = datetime.now(timezone.utc)
        return _run_live_mode(live_mode, now)

    if len(argv) < 1 or argv[0] not in MODES:
        print(f"usage: python -m agent.demo <{'|'.join(MODES)}>")
        return 2
    now = datetime.now(timezone.utc)
    MODES[argv[0]](now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
