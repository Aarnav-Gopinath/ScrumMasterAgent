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
import re
import sys
import webbrowser
from datetime import datetime, timezone
from typing import Optional

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


def _print_scan_progress(result: dict) -> None:
    """`on_repo_scanned` callback for scan_all_repos — prints one line per repo as
    it completes. Called from the main thread only, so prints never interleave."""
    repo_name = result["repo"]
    if result.get("skipped"):
        reason = result.get("reason")
        if reason == "abandoned":
            days = result.get("days")
            days_str = f"{days} days" if days is not None else "no activity"
            print(f"⊘ {repo_name} — skipped (abandoned, {days_str})")
        else:
            print(f"⊘ {repo_name} — skipped (error: {result.get('error')})")
    else:
        print(f"✓ {repo_name} — {len(result['stories'])} issues scanned")


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

    if mode in {"staleness", "standup"}:
        print(f"\nScanning {len(repos)} repo(s) in parallel (max_workers=8)...")
        results = org_client.scan_all_repos(
            config,
            now,
            state=None,
            jira_client=jira_client,
            max_repos=repo_cap,
            max_issues=_MAX_ISSUES_PER_REPO_DEMO,
            max_workers=8,
            on_repo_scanned=_print_scan_progress,
        )

        for result in results:
            repo_name = result["repo"]
            print(f"\n=== {repo_name} ===")
            if result.get("skipped"):
                reason = result.get("reason")
                if reason == "abandoned":
                    days = result.get("days")
                    if days is None:
                        print(
                            f"Skipping {repo_name} — no activity in over "
                            f"{config.abandoned_days} days"
                        )
                    else:
                        print(f"Skipping {repo_name} — no activity in {days} days")
                else:
                    print(f"Skipping {repo_name} — scan error: {result.get('error')}")
                continue

            repo = result["repo_obj"]
            repo_client = GitHubClient(repo)
            notifier = _DryRunNotifier(repo_client, repo_name)
            status_by_number = {story.number: status for story, status, _ in result["stories"]}

            if mode == "staleness":
                summary: list[tuple[int, str]] = []
                for story, status, snapshot in result["stories"]:
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
                        discrepancies = [
                            d for d in result["discrepancies"] if d.issue_number == story.number
                        ]
                        for d in discrepancies:
                            print(
                                f"JIRA DISCREPANCY — would post to issue #{story.number} "
                                f"in {repo_name}:\n"
                                f"{describe_discrepancy(d)}"
                            )
                print(f"summary: {summary}")
            elif mode == "standup":
                repo_active_stories: list[tuple[Story, StoryStatus, object]] = [
                    (story, status, snapshot)
                    for story, status, snapshot in result["stories"]
                    if status in {StoryStatus.IN_PROGRESS, StoryStatus.IN_REVIEW, StoryStatus.STALLED}
                ]
                repo_discrepancies: list[tuple[str, object]] = []
                if jira_client is not None:
                    for d in result["discrepancies"]:
                        if status_by_number.get(d.issue_number) in {
                            StoryStatus.IN_PROGRESS,
                            StoryStatus.STALLED,
                        }:
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
    else:
        for repo in repos:
            repo_name = getattr(repo, "full_name", getattr(repo, "name", "unknown-repo"))
            print(f"\n=== {repo_name} ===")
            repo_client = GitHubClient(repo)
            notifier = _DryRunNotifier(repo_client, repo_name)
            if mode == "pr_watcher":
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


def _apply_inline_markdown(text: str) -> str:
    """**bold** / *em* → <strong>/<em>. `text` must already be HTML-escaped."""
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    return text


def _split_table_row(row: str) -> list[str]:
    """Split a `| a | b |` markdown row into its trimmed cell strings."""
    cells = row.strip()
    if cells.startswith("|"):
        cells = cells[1:]
    if cells.endswith("|"):
        cells = cells[:-1]
    return [c.strip() for c in cells.split("|")]


def _markdown_to_html(markdown_text: str) -> str:
    """Convert a small, known subset of Markdown to HTML for the standup digest.

    Deliberately not a general-purpose Markdown renderer — the digest comes from
    either Claude's output or `_fallback_summary`, both of which only ever use
    headings, bold/italic, horizontal rules, bullet lists, blank-line paragraphs,
    and (occasionally, from Claude) pipe tables. Every line is HTML-escaped before
    any tag is added, so nothing in the source text (e.g. an issue title copied
    into the digest) can inject markup.
    """
    html_escape = _html_module.escape
    html_parts: list[str] = []
    list_buffer: list[str] = []
    table_buffer: list[str] = []
    paragraph_buffer: list[str] = []

    def flush_list() -> None:
        if list_buffer:
            items = "".join(f"<li>{item}</li>" for item in list_buffer)
            html_parts.append(f"<ul>{items}</ul>")
            list_buffer.clear()

    def flush_table() -> None:
        if not table_buffer:
            return
        rows = list(table_buffer)
        table_buffer.clear()
        body_rows = rows
        header_cells = None
        if len(rows) >= 2 and re.fullmatch(r"\|?[\s:|-]+\|?", rows[1]):
            header_cells = [_apply_inline_markdown(c) for c in _split_table_row(rows[0])]
            body_rows = rows[2:]
        parts = ["<table>"]
        if header_cells is not None:
            parts.append("<tr>" + "".join(f"<th>{c}</th>" for c in header_cells) + "</tr>")
        for row in body_rows:
            cells = [_apply_inline_markdown(c) for c in _split_table_row(row)]
            parts.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
        parts.append("</table>")
        html_parts.append("".join(parts))

    def flush_paragraph() -> None:
        if paragraph_buffer:
            html_parts.append("<p>" + " ".join(paragraph_buffer) + "</p>")
            paragraph_buffer.clear()

    for raw_line in markdown_text.split("\n"):
        stripped = html_escape(raw_line.strip())

        if stripped == "":
            flush_list()
            flush_table()
            flush_paragraph()
            continue

        heading2 = re.match(r"^##\s+(.*)$", stripped)
        heading3 = re.match(r"^###\s+(.*)$", stripped)
        is_hr = stripped in ("---", "***", "___")
        list_item = re.match(r"^-\s+(.*)$", stripped)
        is_table_row = stripped.startswith("|") and stripped.endswith("|")

        if heading2:
            flush_list()
            flush_table()
            flush_paragraph()
            html_parts.append(f"<h2>{_apply_inline_markdown(heading2.group(1))}</h2>")
        elif heading3:
            flush_list()
            flush_table()
            flush_paragraph()
            html_parts.append(f"<h3>{_apply_inline_markdown(heading3.group(1))}</h3>")
        elif is_hr:
            flush_list()
            flush_table()
            flush_paragraph()
            html_parts.append("<hr>")
        elif list_item:
            flush_table()
            flush_paragraph()
            list_buffer.append(_apply_inline_markdown(list_item.group(1)))
        elif is_table_row:
            flush_list()
            flush_paragraph()
            table_buffer.append(stripped)
        else:
            flush_list()
            flush_table()
            paragraph_buffer.append(_apply_inline_markdown(stripped))

    flush_list()
    flush_table()
    flush_paragraph()
    return "\n".join(html_parts)


def _build_report_html(
    timestamp_str: str,
    scan_duration: float,
    repos_scanned: int,
    active_repos: int,
    stalled_count: int,
    jira_discrepancy_count: int,
    repos_skipped: int,
    repos_no_issues: int,
    total_issues_scanned: int,
    repo_data: list[dict],
    jira_discrepancies: list[dict],
    standup_digest: str,
    now: datetime,
) -> str:
    """Build HTML report with inline CSS for the live report mode."""
    html_escape = _html_module.escape
    
    # Build repo rows with improvements
    repo_rows = []
    for repo_info in repo_data:
        repo_name = repo_info["repo_name"]
        for issue_info in repo_info["issues"]:
            issue_num = issue_info["number"]
            # Truncate title at 60 chars
            title_full = issue_info["title"]
            title = html_escape(title_full[:60] + "..." if len(title_full) > 60 else title_full)
            assignee = html_escape(issue_info["assignee"])
            status = issue_info["status"]
            last_activity_str = issue_info["last_activity"]
            last_activity_dt = issue_info.get("last_activity_dt")
            action = issue_info["action"]
            
            # Calculate days ago
            if last_activity_dt:
                days_ago = (now - last_activity_dt).days
                last_activity_display = f'{last_activity_str} ({days_ago} days ago)'
            else:
                last_activity_display = last_activity_str
            
            # Status badge color
            status_color = {
                "in_progress": "#28a745",
                "in_review": "#007bff",
                "stalled": "#dc3545",
                "not_started": "#6c757d",
                "done": "#28a745",
            }.get(status, "#6c757d")
            
            # Action color
            action_color = "#6c757d"
            if "Needs attention" in action:
                action_color = "#EF4444"
            elif "Active" in action:
                action_color = "#10B981"
            elif "review" in action.lower():
                action_color = "#3B82F6"
            
            repo_rows.append(
                f'<tr>'
                f'<td>{html_escape(repo_name)}</td>'
                f'<td><strong>#{issue_num}</strong></td>'
                f'<td>{title}</td>'
                f'<td>{assignee}</td>'
                f'<td><span class="badge" style="background-color: {status_color};">{html_escape(status)}</span></td>'
                f'<td>{html_escape(last_activity_display)}</td>'
                f'<td style="color: {action_color}; font-weight: 500;">{html_escape(action)}</td>'
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
    
    standup_html = _markdown_to_html(standup_digest)
    
    # Stat card colors
    stalled_color = "#EF4444" if stalled_count > 0 else "#F59E0B"
    discrepancy_color = "#EF4444" if jira_discrepancy_count > 0 else "#10B981"
    
    # Scan summary
    scan_summary_html = f'''
    <div class="scan-summary">
        <h3>Scan Summary</h3>
        <ul>
            <li>Repos skipped (abandoned): {repos_skipped}</li>
            <li>Repos with no open issues: {repos_no_issues}</li>
            <li>Total issues scanned: {total_issues_scanned}</li>
            <li>Scan cap: 75 repos, 30 issues/repo</li>
        </ul>
    </div>
    '''
    
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
        .header .timestamp {{ font-size: 0.9rem; opacity: 0.9; margin-bottom: 0.25rem; }}
        .header .duration {{ font-size: 0.85rem; opacity: 0.85; }}
        .dry-run-banner {{ background: #ffc107; color: #212529; padding: 1rem; text-align: center; font-weight: bold; }}
        .container {{ max-width: 1400px; margin: 0 auto; padding: 2rem; }}
        .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 1.5rem; margin-bottom: 2rem; }}
        .stat-card {{ background: white; border-radius: 8px; padding: 1.5rem; box-shadow: 0 2px 4px rgba(0,0,0,0.1); border-left: 4px solid; }}
        .stat-card h3 {{ font-size: 0.9rem; color: #6c757d; text-transform: uppercase; margin-bottom: 0.5rem; }}
        .stat-card .value {{ font-size: 2.5rem; font-weight: bold; }}
        .scan-summary {{ background: #F3F4F6; border-radius: 8px; padding: 1.5rem; margin-bottom: 2rem; }}
        .scan-summary h3 {{ font-size: 1rem; color: #002B5C; margin-bottom: 1rem; }}
        .scan-summary ul {{ list-style: none; }}
        .scan-summary li {{ padding: 0.25rem 0; color: #6c757d; }}
        table {{ width: 100%; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 2rem; }}
        thead {{ background: #002B5C; color: white; }}
        th, td {{ padding: 1rem; text-align: left; border-bottom: 1px solid #dee2e6; }}
        th {{ font-weight: 600; }}
        tbody tr:nth-child(even) {{ background: #F8FAFC; }}
        tbody tr:hover {{ background: #f1f5f9; }}
        .badge {{ display: inline-block; padding: 0.25rem 0.75rem; border-radius: 4px; color: white; font-size: 0.85rem; font-weight: 500; text-transform: lowercase; }}
        .digest {{ background: white; border-radius: 8px; padding: 2rem; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 2rem; }}
        .digest h2 {{ color: #002B5C; margin-bottom: 1rem; }}
        .digest table {{ border-collapse: collapse; margin: 1rem 0; }}
        .digest th {{ background: #f1f5f9; padding: 8px 12px; text-align: left; }}
        .digest td {{ padding: 8px 12px; border-bottom: 1px solid #e2e8f0; }}
        .footer {{ text-align: center; padding: 2rem; color: #6c757d; font-size: 0.9rem; line-height: 1.6; }}
        h2 {{ color: #002B5C; margin: 2rem 0 1rem 0; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>UST-PACE Scrum Master Agent</h1>
        <div class="timestamp">Generated: {html_escape(timestamp_str)}</div>
        <div class="duration">Scan completed in {scan_duration:.1f} seconds</div>
    </div>
    <div class="dry-run-banner">DRY RUN MODE — No changes posted to GitHub</div>
    <div class="container">
        <div class="stats">
            <div class="stat-card" style="border-left-color: #3B82F6;">
                <h3>Repos Scanned</h3>
                <div class="value" style="color: #3B82F6;">{repos_scanned}</div>
            </div>
            <div class="stat-card" style="border-left-color: #10B981;">
                <h3>Active Repos</h3>
                <div class="value" style="color: #10B981;">{active_repos}</div>
            </div>
            <div class="stat-card" style="border-left-color: {stalled_color};">
                <h3>Stalled Issues</h3>
                <div class="value" style="color: {stalled_color};">{stalled_count}</div>
            </div>
            <div class="stat-card" style="border-left-color: {discrepancy_color};">
                <h3>Jira Discrepancies</h3>
                <div class="value" style="color: {discrepancy_color};">{jira_discrepancy_count}</div>
            </div>
        </div>
        
        {scan_summary_html}
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
            <div style="font-size: 14px; line-height: 1.7; color: #1e293b;">{standup_html}</div>
        </div>
    </div>
    <div class="footer">
        Generated by Scrum Master Agent — Dry Run<br>
        Next scheduled run: weekdays at 9:00 AM UTC<br>
        Agent repo: Aarnav-Gopinath/ScrumMasterAgent
    </div>
</body>
</html>'''


def _run_live_report(now: datetime, open_browser: bool = True) -> int:
    """Run live report: scan repos, build HTML, open in browser.

    `open_browser=False` lets `live full` defer opening the report until after
    the test comment has been posted, per its documented step order.
    """
    import time
    start_time = time.time()
    
    load_dotenv()
    config = load_config()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN required for live report")
        return 1
    
    org_client = GitHubClient.from_org_token(token=token, org_name=config.org_name)

    # Build Jira client
    jira_client = None
    try:
        from agent.services.jira_client import JiraClient
        jira_client = JiraClient.from_env()
    except Exception:  # noqa: BLE001
        pass

    print("Scanning repos in parallel (max_repos=75, max_issues_per_repo=30, max_workers=8)...")
    results = org_client.scan_all_repos(
        config,
        now,
        state=None,
        jira_client=jira_client,
        max_repos=75,
        max_issues=_MAX_ISSUES_PER_REPO_DEMO,
        max_workers=8,
        on_repo_scanned=_print_scan_progress,
    )

    if not results:
        print("No repos found after exclusions.")
        return 1

    repo_data: list[dict] = []
    jira_discrepancies: list[dict] = []
    active_repos = 0
    stalled_count = 0
    repos_skipped = 0
    repos_no_issues = 0
    total_issues_scanned = 0
    all_active_stories: list[tuple[Story, StoryStatus, object]] = []

    for result in results:
        repo_name = result["repo"]

        if result.get("skipped"):
            repos_skipped += 1
            continue

        stories = result["stories"]
        if len(stories) == 0:
            repos_no_issues += 1
            continue

        total_issues_scanned += len(stories)

        repo_issues = []
        for story, status, snapshot in stories:
            if status not in {StoryStatus.IN_PROGRESS, StoryStatus.IN_REVIEW, StoryStatus.STALLED}:
                continue

            all_active_stories.append((story, status, snapshot))

            assignee = story.assignees[0] if story.assignees else "(unassigned)"
            last_activity_str = (
                snapshot.last_activity_at.strftime("%Y-%m-%d")
                if snapshot.last_activity_at
                else "no activity"
            )

            # Action based on status
            if status == StoryStatus.STALLED:
                action = "Needs attention"
            elif status == StoryStatus.IN_REVIEW:
                action = "Awaiting review"
            else:
                action = "Active"

            repo_issues.append({
                "number": story.number,
                "title": story.title,
                "assignee": assignee,
                "status": status.value,
                "last_activity": last_activity_str,
                "last_activity_dt": snapshot.last_activity_at,
                "action": action,
            })

            if status == StoryStatus.STALLED:
                stalled_count += 1

        # Jira discrepancies (same IN_PROGRESS/STALLED restriction as before).
        if jira_client:
            from agent.models import describe_discrepancy
            status_by_number = {story.number: status for story, status, _ in stories}
            for d in result["discrepancies"]:
                if status_by_number.get(d.issue_number) in {StoryStatus.IN_PROGRESS, StoryStatus.STALLED}:
                    jira_discrepancies.append({
                        "repo_name": repo_name,
                        "issue_number": d.issue_number,
                        "description": describe_discrepancy(d),
                    })

        if repo_issues:
            active_repos += 1
            repo_data.append({"repo_name": repo_name, "issues": repo_issues})

    # Generate standup digest
    digest = generate_standup_summary(all_active_stories) if all_active_stories else "No active stories detected."
    
    # Calculate scan duration
    scan_duration = time.time() - start_time
    
    # Format timestamp properly (cross-platform)
    hour = now.hour if now.hour <= 12 else now.hour - 12
    if hour == 0:
        hour = 12
    am_pm = "AM" if now.hour < 12 else "PM"
    timestamp_formatted = now.strftime(f"%A, %B %d, %Y at {hour}:%M {am_pm} UTC")
    
    # Build HTML
    html_content = _build_report_html(
        timestamp_str=timestamp_formatted,
        scan_duration=scan_duration,
        repos_scanned=len(results),
        active_repos=active_repos,
        stalled_count=stalled_count,
        jira_discrepancy_count=len(jira_discrepancies),
        repos_skipped=repos_skipped,
        repos_no_issues=repos_no_issues,
        total_issues_scanned=total_issues_scanned,
        repo_data=repo_data,
        jira_discrepancies=jira_discrepancies,
        standup_digest=digest,
        now=now,
    )
    
    output_path = "demo-report.html"
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(html_content)
    
    print(f"\n✓ Report generated: {output_path}")
    print(f"  Repos Scanned: {len(results)}")
    print(f"  Active Repos: {active_repos}")
    print(f"  Stalled Issues: {stalled_count}")
    print(f"  Jira Discrepancies: {len(jira_discrepancies)}")
    print(f"  Scan Duration: {scan_duration:.1f}s")
    
    if open_browser:
        webbrowser.open(f"file://{os.path.abspath(output_path)}")
    return 0


# Default shown in the printed setup instructions when TEST_ISSUE_NUMBER is unset —
# update once a real demo issue exists in the target repo.
_TEST_ISSUE_NUMBER_DEFAULT = 2

_TEST_POST_COMMENT_BODY = (
    "👋 Scrum Master Agent — Test Post\n\n"
    "This is a demonstration comment showing the agent's ability to detect "
    "stalled work and notify assignees.\n\n"
    "In production this comment would appear when a story has had no commit "
    "activity for 2+ business days.\n\n"
    f"{AGENT_COMMENT_MARKER}"
)


def _post_test_comment() -> tuple[int, Optional[str]]:
    """Post one real (non-dry-run) comment to a real GitHub issue.

    Entirely env-driven so nothing about the target repo/issue is hardcoded:
      TEST_REPO          e.g. "Aarnav-Gopinath/ScrumMasterAgent"
      TEST_ISSUE_NUMBER  the issue number to comment on
      GITHUB_TOKEN       must have permission to comment on that repo/issue

    Returns (exit_code, comment_url) — shared by `test-post` and `live full`,
    the latter needing the URL for its final summary line.
    """
    load_dotenv()

    test_repo = os.environ.get("TEST_REPO")
    if not test_repo:
        print(
            "TEST_REPO not set.\n\n"
            "Set it to the repo you want to post a real test comment to, then "
            "re-run, e.g.:\n"
            "  export TEST_REPO=Aarnav-Gopinath/ScrumMasterAgent\n"
            f"  export TEST_ISSUE_NUMBER={_TEST_ISSUE_NUMBER_DEFAULT}\n"
            "  python -m agent.demo test-post"
        )
        return 1, None

    test_issue_raw = os.environ.get("TEST_ISSUE_NUMBER")
    if not test_issue_raw:
        print(
            "TEST_ISSUE_NUMBER not set.\n\n"
            "Set it to the issue number to post the test comment on, then re-run, "
            "e.g.:\n"
            f"  export TEST_REPO={test_repo}\n"
            f"  export TEST_ISSUE_NUMBER={_TEST_ISSUE_NUMBER_DEFAULT}\n"
            "  python -m agent.demo test-post"
        )
        return 1, None

    try:
        test_issue_number = int(test_issue_raw)
    except ValueError:
        print(f"TEST_ISSUE_NUMBER must be an integer, got: {test_issue_raw!r}")
        return 1, None

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN required for test-post")
        return 1, None

    client = GitHubClient.from_token(test_repo, token=token)
    issue = client.get_issue(test_issue_number)
    print(f"Fetched issue #{test_issue_number}: {issue.title}")

    # Idempotency: skip posting if the agent has already commented on this
    # issue (same marker the staleness/notifier code uses to recognize its
    # own comments), so re-running the demo doesn't spam duplicate comments.
    for existing in client.get_comments(test_issue_number):
        if AGENT_COMMENT_MARKER in (getattr(existing, "body", "") or ""):
            existing_url = getattr(existing, "html_url", None)
            print(f"✓ Demo comment already exists on issue #{test_issue_number} — skipping post")
            print(f"  View it at: {existing_url}")
            return 0, existing_url

    comment = client.post_comment(test_issue_number, _TEST_POST_COMMENT_BODY)

    print(f"Comment URL: {comment.html_url}")
    print("✓ Comment posted successfully — open the issue to see it live")
    return 0, comment.html_url


def _run_test_post() -> int:
    exit_code, _comment_url = _post_test_comment()
    return exit_code


def _run_live_full(now: datetime) -> int:
    """Run the full demo end-to-end: live report scan, then a real test comment,
    then open the report — so a viewer sees the report and the live comment
    land in the same pass."""
    print("Step 1/2 — Generating live report...")
    report_exit = _run_live_report(now, open_browser=False)
    if report_exit != 0:
        return report_exit

    print("\nStep 2/2 — Posting demo comment...")
    post_exit, comment_url = _post_test_comment()
    if post_exit != 0:
        return post_exit

    webbrowser.open(f"file://{os.path.abspath('demo-report.html')}")

    print(f"\n✓ Demo complete — report open in browser, comment posted at: {comment_url}")
    return 0


MODES = {
    "status": demo_status,
    "staleness": demo_staleness,
    "standup": demo_standup,
    "pr_watcher": demo_pr_watcher,
    "completion": demo_completion,
}


def main(argv: list[str]) -> int:
    if len(argv) >= 1 and argv[0] == "test-post":
        return _run_test_post()

    if len(argv) >= 2 and argv[0] == "live":
        live_mode = argv[1]
        if live_mode == "report":
            now = datetime.now(timezone.utc)
            return _run_live_report(now)
        if live_mode == "full":
            now = datetime.now(timezone.utc)
            return _run_live_full(now)
        if live_mode not in {"staleness", "standup", "pr_watcher", "completion"}:
            print(
                "usage: python -m agent.demo live "
                "<staleness|standup|pr_watcher|completion|report|full>"
            )
            return 2
        now = datetime.now(timezone.utc)
        return _run_live_mode(live_mode, now)

    if len(argv) < 1 or argv[0] not in MODES:
        print(f"usage: python -m agent.demo <{'|'.join(MODES)}|test-post>")
        return 2
    now = datetime.now(timezone.utc)
    MODES[argv[0]](now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
