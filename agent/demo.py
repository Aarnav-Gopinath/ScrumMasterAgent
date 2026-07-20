"""Offline demo runner: `python -m agent.demo <mode>`.

Wires a GitHubClient over the in-memory FixtureRepo (no network, no tokens) and runs
a sub-agent against the JSON fixtures, printing what it reads and what it *would* post
(captured on repo.posted_comments). This is the per-phase "see it work" checkpoint.

Modes are added phase by phase: issues, status, staleness, standup, pr_watcher,
completion.
"""

from __future__ import annotations

import html as _html_module
import json
import logging
import os
import sys
import webbrowser
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
# Cap per-repo issue scan in demo/live mode to keep runtime manageable.
_MAX_ISSUES_PER_REPO_DEMO = 30
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

    # Build Jira client once — shared across repos for live modes.
    jira_client = None
    if mode in {"staleness", "standup"}:
        try:
            from agent.services.jira_client import JiraClient
            jira_client = JiraClient.from_env()
            print("Jira client: connected.")
        except Exception as exc:  # noqa: BLE001
            print(f"Jira client unavailable — discrepancy detection disabled: {exc}")

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
            issues = repo_client.get_open_issues(repo)
            if len(issues) > _MAX_ISSUES_PER_REPO_DEMO:
                print(
                    f"  (capping to first {_MAX_ISSUES_PER_REPO_DEMO} of "
                    f"{len(issues)} open issues — demo mode)"
                )
                issues = issues[:_MAX_ISSUES_PER_REPO_DEMO]
            for issue in issues:
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

                # Jira discrepancy dry-run output for stalled stories.
                if jira_client is not None:
                    from agent.models import describe_discrepancy
                    from agent.services.metrics import detect_jira_discrepancies
                    discrepancies = detect_jira_discrepancies(
                        story, snapshot, status, jira_client, config.staleness_days, now
                    )
                    for d in discrepancies:
                        print(
                            f"JIRA DISCREPANCY — would post to issue #{story.number} "
                            f"in {repo_name}:\n"
                            f"{describe_discrepancy(d)}"
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
            repo_discrepancies: list[tuple[str, object]] = []
            issues = repo_client.get_open_issues(repo)
            if len(issues) > _MAX_ISSUES_PER_REPO_DEMO:
                print(
                    f"  (capping to first {_MAX_ISSUES_PER_REPO_DEMO} of "
                    f"{len(issues)} open issues — demo mode)"
                )
                issues = issues[:_MAX_ISSUES_PER_REPO_DEMO]
            for issue in issues:
                story = Story.from_issue(issue)
                snapshot = build_activity_snapshot(repo_client, story, repo=repo)
                status = infer_status(
                    story, snapshot, config.staleness_days, config.business_days_only, now
                )
                if status in {StoryStatus.IN_PROGRESS, StoryStatus.IN_REVIEW, StoryStatus.STALLED}:
                    repo_active_stories.append((story, status, snapshot))
                if jira_client is not None and status in {StoryStatus.IN_PROGRESS, StoryStatus.STALLED}:
                    from agent.models import describe_discrepancy
                    from agent.services.metrics import detect_jira_discrepancies
                    discs = detect_jira_discrepancies(
                        story, snapshot, status, jira_client, config.staleness_days, now
                    )
                    for d in discs:
                        repo_discrepancies.append((repo_name, d))

            print(f"=== {repo_name} ({len(repo_active_stories)} active stories) ===")
            for story, status, _snapshot in repo_active_stories:
                print(f"  - #{story.number} {story.title} [{status.value}]")

            if repo_discrepancies:
                print(f"\nJira discrepancies in {repo_name}:")
                for _rname, d in repo_discrepancies:
                    from agent.models import describe_discrepancy
                    print(
                        f"  JIRA DISCREPANCY — would post to issue #{d.issue_number} "
                        f"in {repo_name}:\n"
                        f"  {describe_discrepancy(d)}"
                    )

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


def _build_report_html(
    timestamp: str,
    repos_scanned: int,
    active_repos: int,
    stalled_count: int,
    jira_discrepancy_count: int,
    repo_data: list[dict],
    jira_discrepancies: list[dict],
    standup_digest: str,
) -> str:
    """Build HTML report with inline CSS for the live report mode."""
    html_escape = _html_module.escape
    
    # Build repo rows
    repo_rows = []
    for repo_info in repo_data:
        repo_name = repo_info["repo_name"]
        for issue_info in repo_info["issues"]:
            issue_num = issue_info["number"]
            title = html_escape(issue_info["title"])
            assignee = html_escape(issue_info["assignee"])
            status = issue_info["status"]
            last_activity = html_escape(issue_info["last_activity"])
            action = html_escape(issue_info["action"])
            
            # Status badge color
            status_color = {
                "in_progress": "#28a745",
                "in_review": "#007bff",
                "stalled": "#dc3545",
                "not_started": "#6c757d",
                "done": "#28a745",
            }.get(status, "#6c757d")
            
            repo_rows.append(
                f'<tr>'
                f'<td>{html_escape(repo_name)}</td>'
                f'<td>#{issue_num}</td>'
                f'<td>{title}</td>'
                f'<td>{assignee}</td>'
                f'<td><span class="badge" style="background-color: {status_color};">{html_escape(status)}</span></td>'
                f'<td>{last_activity}</td>'
                f'<td>{action}</td>'
                f'</tr>'
            )
    
    repo_table = "\n".join(repo_rows) if repo_rows else '<tr><td colspan="7" style="text-align: center;">No issues found</td></tr>'
    
    # Build Jira discrepancy rows
    jira_rows = []
    for disc in jira_discrepancies:
        repo_name = html_escape(disc["repo_name"])
        issue_num = disc["issue_number"]
        description = html_escape(disc["description"])
        jira_rows.append(
            f'<tr>'
            f'<td>{repo_name}</td>'
            f'<td>#{issue_num}</td>'
            f'<td>{description}</td>'
            f'</tr>'
        )
    
    jira_table_html = ""
    if jira_rows:
        jira_table = "\n".join(jira_rows)
        jira_table_html = f'''
        <h2>Jira Discrepancies</h2>
        <table>
            <thead>
                <tr>
                    <th>Repository</th>
                    <th>Issue</th>
                    <th>Discrepancy</th>
                </tr>
            </thead>
            <tbody>
                {jira_table}
            </tbody>
        </table>
        '''
    
    standup_html = html_escape(standup_digest).replace("\n", "<br>")
    
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>UST-PACE Scrum Master Agent Report</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; background: #f8f9fa; color: #212529; }}
        .header {{ background: #002B5C; color: white; padding: 2rem; text-align: center; }}
        .header h1 {{ font-size: 2rem; margin-bottom: 0.5rem; }}
        .header .timestamp {{ font-size: 0.9rem; opacity: 0.9; }}
        .dry-run-banner {{ background: #ffc107; color: #212529; padding: 1rem; text-align: center; font-weight: bold; }}
        .container {{ max-width: 1400px; margin: 0 auto; padding: 2rem; }}
        .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 1.5rem; margin-bottom: 2rem; }}
        .stat-card {{ background: white; border-radius: 8px; padding: 1.5rem; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .stat-card h3 {{ font-size: 0.9rem; color: #6c757d; text-transform: uppercase; margin-bottom: 0.5rem; }}
        .stat-card .value {{ font-size: 2.5rem; font-weight: bold; color: #002B5C; }}
        table {{ width: 100%; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 2rem; }}
        thead {{ background: #002B5C; color: white; }}
        th, td {{ padding: 1rem; text-align: left; border-bottom: 1px solid #dee2e6; }}
        th {{ font-weight: 600; }}
        tbody tr:hover {{ background: #f8f9fa; }}
        .badge {{ display: inline-block; padding: 0.25rem 0.75rem; border-radius: 4px; color: white; font-size: 0.85rem; font-weight: 500; text-transform: lowercase; }}
        .digest {{ background: white; border-radius: 8px; padding: 2rem; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 2rem; }}
        .digest h2 {{ color: #002B5C; margin-bottom: 1rem; }}
        .footer {{ text-align: center; padding: 2rem; color: #6c757d; font-size: 0.9rem; }}
        h2 {{ color: #002B5C; margin: 2rem 0 1rem 0; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>UST-PACE Scrum Master Agent</h1>
        <div class="timestamp">{html_escape(timestamp)}</div>
    </div>
    <div class="dry-run-banner">DRY RUN MODE — No changes posted to GitHub</div>
    <div class="container">
        <div class="stats">
            <div class="stat-card">
                <h3>Repos Scanned</h3>
                <div class="value">{repos_scanned}</div>
            </div>
            <div class="stat-card">
                <h3>Active Repos</h3>
                <div class="value">{active_repos}</div>
            </div>
            <div class="stat-card">
                <h3>Stalled Issues</h3>
                <div class="value">{stalled_count}</div>
            </div>
            <div class="stat-card">
                <h3>Jira Discrepancies</h3>
                <div class="value">{jira_discrepancy_count}</div>
            </div>
        </div>
        
        <h2>Repository Issues</h2>
        <table>
            <thead>
                <tr>
                    <th>Repository</th>
                    <th>Issue</th>
                    <th>Title</th>
                    <th>Assignee</th>
                    <th>Status</th>
                    <th>Last Activity</th>
                    <th>Action</th>
                </tr>
            </thead>
            <tbody>
                {repo_table}
            </tbody>
        </table>
        
        {jira_table_html}
        
        <div class="digest">
            <h2>Standup Digest</h2>
            <div>{standup_html}</div>
        </div>
    </div>
    <div class="footer">
        Generated by Scrum Master Agent — Dry Run
    </div>
</body>
</html>'''


def _run_live_report(now: datetime) -> int:
    """Run live report: scan repos, build HTML, open in browser."""
    load_dotenv()
    config = load_config()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN required for live report")
        return 1
    
    org_client = GitHubClient.from_org_token(token=token, org_name=config.org_name)
    repos = org_client.get_org_repos(exclude_repo=config.agent_repo, max_repos=75)
    
    if not repos:
        print("No repos found after exclusions.")
        return 1
    
    print(f"Scanning {len(repos)} repos (max_repos=75, max_issues_per_repo=30)...")
    
    # Build Jira client
    jira_client = None
    try:
        from agent.services.jira_client import JiraClient
        jira_client = JiraClient.from_env()
    except Exception:  # noqa: BLE001
        pass
    
    repo_data: list[dict] = []
    jira_discrepancies: list[dict] = []
    active_repos = 0
    stalled_count = 0
    all_active_stories: list[tuple[Story, StoryStatus, object]] = []
    
    for repo in repos:
        repo_name = getattr(repo, "full_name", getattr(repo, "name", "unknown-repo"))
        repo_client = GitHubClient(repo)
        
        last_activity = org_client.get_last_repo_activity(repo)
        if is_repo_abandoned(last_activity, now, config.abandoned_days):
            continue
        
        issues = repo_client.get_open_issues(repo)
        if len(issues) > _MAX_ISSUES_PER_REPO_DEMO:
            issues = issues[:_MAX_ISSUES_PER_REPO_DEMO]
        
        repo_issues = []
        for issue in issues:
            story = Story.from_issue(issue)
            snapshot = build_activity_snapshot(repo_client, story, repo=repo)
            status = infer_status(
                story, snapshot, config.staleness_days, config.business_days_only, now
            )
            
            if status not in {StoryStatus.IN_PROGRESS, StoryStatus.IN_REVIEW, StoryStatus.STALLED}:
                continue
            
            all_active_stories.append((story, status, snapshot))
            
            assignee = story.assignees[0] if story.assignees else "(unassigned)"
            last_activity_str = (
                snapshot.last_activity_at.strftime("%Y-%m-%d")
                if snapshot.last_activity_at
                else "no activity"
            )
            action = "Monitor" if status != StoryStatus.STALLED else "Needs attention"
            
            repo_issues.append({
                "number": story.number,
                "title": story.title,
                "assignee": assignee,
                "status": status.value,
                "last_activity": last_activity_str,
                "action": action,
            })
            
            if status == StoryStatus.STALLED:
                stalled_count += 1
            
            # Jira discrepancies
            if jira_client and status in {StoryStatus.IN_PROGRESS, StoryStatus.STALLED}:
                from agent.models import describe_discrepancy
                from agent.services.metrics import detect_jira_discrepancies
                discs = detect_jira_discrepancies(
                    story, snapshot, status, jira_client, config.staleness_days, now
                )
                for d in discs:
                    jira_discrepancies.append({
                        "repo_name": repo_name,
                        "issue_number": story.number,
                        "description": describe_discrepancy(d),
                    })
        
        if repo_issues:
            active_repos += 1
            repo_data.append({"repo_name": repo_name, "issues": repo_issues})
    
    # Generate standup digest
    digest = generate_standup_summary(all_active_stories) if all_active_stories else "No active stories detected."
    
    # Build HTML
    html_content = _build_report_html(
        timestamp=now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        repos_scanned=len(repos),
        active_repos=active_repos,
        stalled_count=stalled_count,
        jira_discrepancy_count=len(jira_discrepancies),
        repo_data=repo_data,
        jira_discrepancies=jira_discrepancies,
        standup_digest=digest,
    )
    
    output_path = "demo-report.html"
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(html_content)
    
    print(f"\n✓ Report generated: {output_path}")
    print(f"  Repos Scanned: {len(repos)}")
    print(f"  Active Repos: {active_repos}")
    print(f"  Stalled Issues: {stalled_count}")
    print(f"  Jira Discrepancies: {len(jira_discrepancies)}")
    
    # Open in browser
    webbrowser.open(f"file://{os.path.abspath(output_path)}")
    return 0


MODES = {
    "status": demo_status,
    "staleness": demo_staleness,
    "standup": demo_standup,
    "pr_watcher": demo_pr_watcher,
    "completion": demo_completion,
}


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "live":
        live_mode = argv[1]
        if live_mode == "report":
            now = datetime.now(timezone.utc)
            return _run_live_report(now)
        if live_mode not in {"staleness", "standup", "pr_watcher", "completion"}:
            print("usage: python -m agent.demo live <staleness|standup|pr_watcher|completion|report>")
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
