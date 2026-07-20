# Scrum Master Agent

A GitHub-native Scrum Master agent. It watches issues and PRs and, on a
schedule or in response to events, nudges stale stories, posts a natural-language
standup digest, flags PRs waiting for review, and catches issues closed without meeting
the definition of done.

Everything runs on **GitHub Actions** with state stored **in the repo itself** — no
external database, server, or queue for the POC.

## What it does

| Sub-agent | Trigger | Action |
|---|---|---|
| **Staleness Monitor** | cron, weekday mornings | @mentions the assignee of any stalled story (idempotent — never double-reminds) |
| **Standup Reporter** | cron, weekday mornings | Aggregates story statuses, asks Claude for a digest, posts it to a standup issue |
| **PR Review Watcher** | PR opened / ready for review | Notes the review on the referenced issue; a sweep pings PRs left open too long |
| **Story Completion Checker** | issue closed | Flags closures missing a linked PR or the required `status: done` label |

Only the Standup Reporter spends LLM tokens; the rest is pure logic. If the Claude call
fails or no API key is set, the reporter falls back to a deterministic text digest.

## Architecture

A lightweight **orchestrator** (`agent/orchestrator.py`) routes by `AGENT_MODE` to one
of four **sub-agents** (`agent/subagents/`), all built on a shared **service layer**
(`agent/services/`): `config`, `github_client`, `models`, `metrics`, `state`,
`notifier`, and `llm`.

`GitHubClient` wraps a PyGitHub `Repository`, but the repo is **injected** — tests and
the offline demo pass an in-memory `FixtureRepo` (`agent/services/fixtures.py`) that
exposes the same surface, so every code path runs with **zero network access**.

## Running it locally

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # then fill in credentials
```

Run a sub-agent:

```bash
AGENT_MODE=staleness python -m agent.orchestrator
AGENT_MODE=standup   python -m agent.orchestrator
```

## Running against UST-PACE

Populate `.env` with:

- `GITHUB_TOKEN`
- `ANTHROPIC_API_KEY`
- `JIRA_BASE_URL`
- `JIRA_EMAIL`
- `JIRA_API_TOKEN`

`GITHUB_TOKEN` should be a fine-grained PAT scoped to UST-PACE with repository and organization read permissions.

The agent monitors **all open issues across all repos** in the org (excluding its own repo), so no milestone configuration is required.

Commands:

```bash
python -m agent.demo staleness       # fixture demo
python -m agent.demo live staleness  # live org-wide dry run
pytest -m live                        # live smoke tests only
```

⚠️ Warning: disabling dry-run behavior will post real comments on real issues across UST-PACE repos.

### Offline demo (no tokens, no network)

The demo runs each sub-agent against JSON fixtures and prints what it *would* post:

```bash
python -m agent.demo issues       # list fixture stories
python -m agent.demo status       # classify every story's status
python -m agent.demo staleness    # run it twice to see idempotent skips
python -m agent.demo standup      # fallback digest (no API key needed)
python -m agent.demo pr_watcher   # PR-opened event -> note on the issue
python -m agent.demo completion   # bad-close event -> flag comment
```

### Tests

```bash
pytest -v
```

All logic tests run offline via `FixtureRepo` — no GitHub credentials required.

## Configuration

Edit `config.yml`:

```yaml
org_name: "UST-PACE"
agent_repo: "UST-PACE/scrum-master-agent"
staleness_days: 2
business_days_only: true          # weekends don't count toward staleness
standup_issue_number: 1           # create an issue titled "Daily Standup" first
completion_labels:
  - "status: done"
slack_webhook_url: ""             # unused in the POC
jira_base_url: ""                 # optional, falls back to JIRA_BASE_URL env var
```

### Team conventions the agent depends on

GitHub has no native "commits for issue #42" API, so the agent **infers** activity from
references. The team must:

- Reference the issue in commit messages: `add form validation #42`
- Reference it in the PR body: `Closes #42`

Without this, the agent can't link work to stories. This is an operational dependency,
not a bug.

## Deploying (GitHub Actions)

Four workflows live in `.github/workflows/`. Add `ANTHROPIC_API_KEY` as a repo secret
(`GITHUB_TOKEN` is provided automatically). Each workflow grants the minimum
permissions it needs; the staleness workflow also has `contents: write` so it can commit
`agent-state.json` — the agent's memory — back to the repo between runs.

Validate with the **Run workflow** button (`workflow_dispatch`) before relying on the
cron schedules.

## Hidden agent-comment marker

Every comment the agent posts carries an invisible HTML marker
(`<!-- scrum-master-agent -->`). Activity detection ignores marked comments, so a
staleness reminder is never mistaken for developer activity — otherwise the agent's own
nudge would make a stalled story look active and it would stop following up.

## Production notes

- **State**: `agent-state.json` in the repo works for the POC. For scale, swap
  `agent/services/state.py` for SQLite/Postgres (the interface stays the same).
- **Rate limits / cost**: activity detection issues several reads per story. For a large
  repo, cache the commit/PR scans or use a GraphQL batch query — see the notes in
  `github_client.py` and `metrics.py`.

## Production Deployment Checklist

[ ] Transfer repo to UST-PACE via
    GitHub Settings → Danger Zone → Transfer repository  
[ ] Create a GitHub issue titled "Daily Standup" in the
    new UST-PACE agent repo and update
    standup_issue_number in config.yml  
[ ] Populate teams: section in config.yml with team
    names, webhook URLs, and repo lists once Teams
    channels and mapping are confirmed  
[ ] Set these secrets in the UST-PACE repo or org:  
      ANTHROPIC_API_KEY  
      JIRA_BASE_URL  
      JIRA_EMAIL  
      JIRA_API_TOKEN  
      TEAMS_WEBHOOK_URL (if using single channel)  
    (GITHUB_TOKEN is provided automatically by Actions)  
[ ] Enable GitHub Actions in the transferred repo  
[ ] Run each workflow manually via workflow_dispatch
    to verify end-to-end before relying on the schedule  
[ ] Confirm agent-state.json is committed back after
    each staleness run  
[ ] Run python -m agent.scripts.build_teams_mapping
    to auto-populate teams_users.yml with org member
    emails
