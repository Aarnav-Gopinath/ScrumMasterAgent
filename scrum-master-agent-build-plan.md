# Scrum Master Agent — Complete Build Plan

A step-by-step guide to building the GitHub-native Scrum Master Agent from empty repo to working POC. Work through it in order. Each phase produces something you can demo.

---

## Part 1 — Tech Stack (Locked In)

| Layer | Choice | Why |
|---|---|---|
| Language | **Python 3.11+** | Best GitHub + AI tooling, beginner-friendly |
| GitHub API | **PyGitHub** (REST) + raw **GraphQL** for batch queries | PyGitHub for 90% of calls; GraphQL only when rate limits bite |
| AI layer | **Anthropic Claude API** (`claude-sonnet-4-6`) | Generates the natural-language standup digest |
| Scheduler / Runtime | **GitHub Actions** (cron + event triggers) | Runs on GitHub's servers, no external host needed |
| State store | **GitHub Issues + labels + a JSON state file in the repo** | Zero external DB for the POC |
| Config | **`config.yml`** in repo | Change thresholds without touching code |
| Dependency mgmt | **`requirements.txt`** + venv | Standard Python |
| Testing | **pytest** + **GitHub API mocking** | Test logic without hitting real GitHub |

**Decision rule for the POC:** if a choice adds an external service (a hosted database, a separate server, a message queue), don't do it. Everything lives in GitHub until the POC is validated.

---

## Part 2 — Architecture

### 2.1 The Multi-Agent Design

Per your manager's guidance, this is split into focused sub-agents coordinated by a lightweight orchestrator. Each only runs when relevant, which limits API calls and Claude tokens.

```
                    ┌──────────────────┐
                    │   Orchestrator   │   (entry point: main.py)
                    │  routes by event │
                    └────────┬─────────┘
                             │
        ┌────────────┬───────┴───────┬─────────────┐
        ▼            ▼               ▼             ▼
  ┌──────────┐ ┌──────────┐  ┌────────────┐ ┌────────────┐
  │Staleness │ │PR Review │  │   Story    │ │  Standup   │
  │ Monitor  │ │ Watcher  │  │ Completion │ │  Reporter  │
  └──────────┘ └──────────┘  └────────────┘ └────────────┘
   cron daily   event: PR      event: issue    cron daily
                opened         closed          morning
        │            │               │             │
        └────────────┴───────┬───────┴─────────────┘
                             ▼
              ┌──────────────────────────────┐
              │   Shared Service Layer       │
              │  github_client · state ·     │
              │  metrics · notifier · llm    │
              └──────────────────────────────┘
```

### 2.2 What Each Sub-Agent Does

**Staleness Monitor** (cron, daily) — finds in-progress issues with no commit/PR/comment activity in N days, posts an @mention reminder. Reads/writes state to avoid re-pinging.

**PR Review Watcher** (event: `pull_request` opened/ready_for_review) — when a PR is opened, notes which story it belongs to; if a PR has been open beyond a threshold with no review, pings reviewers.

**Story Completion Checker** (event: `issues` closed) — when an issue closes, verifies it has a linked PR and required labels; comments if closure criteria aren't met.

**Standup Reporter** (cron, every morning) — aggregates the sprint's story statuses into structured data, sends it to Claude, posts the natural-language digest to a designated standup issue (or Slack).

### 2.3 The Shared Service Layer

Each sub-agent calls these shared modules so logic isn't duplicated:

- `github_client.py` — all GitHub reads/writes
- `state.py` — read/write the agent's memory (JSON file + labels)
- `metrics.py` — staleness scoring, status inference, activity counting
- `notifier.py` — post comments, @mentions, (optional) Slack
- `llm.py` — call the Claude API, build prompts
- `config.py` — load `config.yml`

---

## Part 3 — Repo Structure

Create this exact layout:

```
scrum-master-agent/
├── .github/
│   └── workflows/
│       ├── staleness.yml          # cron daily → Staleness Monitor
│       ├── standup.yml            # cron daily AM → Standup Reporter
│       ├── pr-watcher.yml         # on PR opened → PR Review Watcher
│       └── completion.yml         # on issue closed → Story Completion
├── agent/
│   ├── __init__.py
│   ├── orchestrator.py            # routes to the right sub-agent
│   ├── subagents/
│   │   ├── __init__.py
│   │   ├── staleness.py
│   │   ├── pr_watcher.py
│   │   ├── completion.py
│   │   └── reporter.py
│   ├── services/
│   │   ├── __init__.py
│   │   ├── github_client.py
│   │   ├── state.py
│   │   ├── metrics.py
│   │   ├── notifier.py
│   │   ├── llm.py
│   │   └── config.py
│   └── models.py                  # dataclasses: Story, ActivitySnapshot, etc.
├── tests/
│   ├── test_metrics.py
│   ├── test_state.py
│   └── test_staleness.py
├── agent-state.json               # the agent's memory (committed by Actions)
├── config.yml
├── requirements.txt
├── .env.example
└── README.md
```

---

## Part 4 — Build Order (12 Steps)

Build in this sequence. Don't skip ahead — each step depends on the last, and each gives you something testable.

| Step | What you build | Demo milestone |
|---|---|---|
| 1 | Repo + config + auth | Script connects to GitHub, prints repo name |
| 2 | `github_client.py` — read issues | Prints all open issues + assignees |
| 3 | `models.py` — data shapes | Issues map cleanly into `Story` objects |
| 4 | `metrics.py` — activity detection | Prints "days since last activity" per story |
| 5 | `metrics.py` — status inference | Each story labeled Not Started / In Progress / Stalled / In Review / Done |
| 6 | `state.py` — memory | Agent records what it's done in `agent-state.json` |
| 7 | `notifier.py` — comments | Agent posts a test comment with an @mention |
| 8 | **Staleness Monitor** sub-agent | Stale stories get reminder comments (no duplicates) |
| 9 | `llm.py` — Claude integration | Structured data → natural-language summary |
| 10 | **Standup Reporter** sub-agent | Posts a real standup digest to an issue |
| 11 | **PR Watcher** + **Completion** sub-agents | Event-driven agents fire correctly |
| 12 | GitHub Actions workflows | Everything runs automatically on schedule/events |

**Build the whole thing as a single script first if multi-agent feels overwhelming** — get steps 1–10 working in one file, validate the logic, *then* split into sub-agents (step 11) and wire up Actions (step 12). Debugging coordination and logic at the same time is the #1 way to get stuck.

---

## Part 5 — Copilot Prompts (Step by Step)

These are written to paste into GitHub Copilot Chat (or Claude Code). Each assumes the previous steps exist. Give Copilot the prompt, then **read the output and make sure you understand it** before moving on — that's the whole point of doing it this way.

> **How to use these:** Open the target file, paste the prompt into Copilot Chat, review what it generates, ask follow-ups if anything is unclear, then commit. Don't paste all 12 at once.

### Step 1 — Project scaffold + auth

```
Create a Python project for a GitHub automation agent. I need:
1. A requirements.txt with PyGitHub, anthropic, PyYAML, python-dotenv, and pytest.
2. A config.py module that loads a config.yml file from the repo root and
   exposes the values as a typed dataclass. Config fields: repo_name (str),
   staleness_days (int, default 2), sprint_milestone (str), standup_issue_number
   (int), business_days_only (bool, default true).
3. A .env.example showing GITHUB_TOKEN and ANTHROPIC_API_KEY.
4. A github_client.py that authenticates to GitHub using PyGitHub with a token
   from the environment, and has a method get_repo() returning the configured repo.
Include error handling if the token is missing. Add docstrings explaining each part.
```

### Step 2 — Read issues

```
In github_client.py, add methods to a GitHubClient class:
- get_open_issues(milestone_name): return all open issues attached to the milestone
  with the given title. If milestone_name is None, return all open issues.
- get_issue(number): return a single issue by number.
Filter out pull requests (PyGitHub returns PRs as issues too — exclude anything
where issue.pull_request is not None).
Return PyGitHub Issue objects for now. Add a docstring noting that PRs are excluded
and why. Write a small __main__ block that prints each issue's number, title, and
assignee logins so I can test it.
```

### Step 3 — Data models

```
Create models.py with Python dataclasses representing the agent's domain:
- Story: number, title, assignees (list of str), labels (list of str), state
  (open/closed), created_at, milestone (str or None).
- ActivitySnapshot: last_commit_at, last_pr_at, last_comment_at, commit_count,
  pr_count, comment_count, and a computed property last_activity_at that returns
  the most recent of the three timestamps (handling None values safely).
- StoryStatus: an Enum with NOT_STARTED, IN_PROGRESS, STALLED, IN_REVIEW, DONE.
Add a from_issue() classmethod on Story that builds a Story from a PyGitHub issue
object. Use datetime for all timestamps. Add type hints throughout.
```

### Step 4 — Activity detection

```
In metrics.py, write a function build_activity_snapshot(client, story) that returns
an ActivitySnapshot for a given Story. To find activity linked to the story:
- Commits: search the repo's commits for any whose message contains "#<number>"
  or whose branch name contains the issue number. (Note in a comment that GitHub
  has no native commit-to-issue API, so we infer via references.)
- PRs: find open and recently closed PRs whose body or title references "#<number>".
- Comments: get the issue's comments and their timestamps.
Count each and capture the most recent timestamp of each type.
Be mindful of GitHub API rate limits — add a comment explaining where caching would
go later. Handle the case where a commit message is None.
```

### Step 5 — Status inference

```
In metrics.py, write infer_status(story, snapshot, staleness_days) that returns a
StoryStatus enum value using these rules:
- If the issue is closed → DONE.
- If open, has a linked PR (snapshot.pr_count > 0) → IN_REVIEW.
- If open, has an assignee, and last_activity_at is within staleness_days → IN_PROGRESS.
- If open, has an assignee, but last_activity_at is older than staleness_days
  (or there's no activity at all) → STALLED.
- If open and has no assignee → NOT_STARTED.
Use the business_days_only config flag: if true, compute the staleness gap counting
only weekdays. Write a helper business_days_between(start, end) for this. Add unit-
test-friendly pure functions (no GitHub calls inside infer_status).
```

### Step 6 — State / memory

```
Create state.py managing the agent's memory in a JSON file (agent-state.json).
Structure: a dict keyed by issue number, each value holding:
  last_reminder_sent_at (ISO timestamp or null),
  reminder_count (int),
  last_status (string).
Functions:
- load_state(path) → dict (return empty dict if file missing).
- save_state(path, state).
- should_remind(state, issue_number, staleness_days, now): returns True only if no
  reminder was sent within the last staleness_days window — so we don't spam.
- record_reminder(state, issue_number, now): updates timestamp and increments count.
Use the standard json and datetime libraries. Make timestamps ISO 8601 strings.
Explain in a docstring why state lives in a committed file for the POC and what we'd
swap to (SQLite/Postgres) for production.
```

### Step 7 — Notifier

```
Create notifier.py with a Notifier class wrapping a GitHubClient.
Methods:
- post_comment(issue_number, body): posts a comment on the issue.
- remind_assignee(story, days_stale): posts a comment that @mentions each assignee
  by login and says they haven't had activity in <days_stale> days. Keep the tone
  friendly and professional. If the story has no assignee, post a comment tagging
  no one that flags the story as unassigned-and-stale instead.
- (stub) post_to_slack(text): leave a clearly-marked TODO that would POST to a Slack
  webhook URL from config; don't implement the HTTP call yet.
Return the created comment object so callers can log the URL.
```

### Step 8 — Staleness Monitor sub-agent

```
Create subagents/staleness.py with a run(client, config, state) function that:
1. Loads all open issues in the sprint milestone.
2. Builds an ActivitySnapshot for each and infers status.
3. For each STALLED story, checks should_remind() from state.py.
4. If it should remind, calls notifier.remind_assignee() and record_reminder().
5. Saves state at the end.
6. Returns a summary list of (issue_number, action_taken) for logging.
Make it idempotent: running it twice in the same day must not double-remind.
Add clear logging (use the logging module, not print) for each decision.
```

### Step 9 — Claude integration

```
Create llm.py with a function generate_standup_summary(stories_with_status) that
takes a list of (Story, StoryStatus, ActivitySnapshot) tuples and returns a natural-
language standup report as a string.
- Use the anthropic Python SDK with model "claude-sonnet-4-6".
- Build a structured prompt: a system message telling Claude it's a scrum master
  summarizing sprint status concisely, and a user message containing the story data
  as compact JSON.
- Ask for: a 2-3 sentence overview, then a short per-developer breakdown of what's in
  progress / stalled / in review, and an explicit "Needs attention" section listing
  stalled stories.
- Keep max_tokens reasonable (around 1024). Handle API errors gracefully and return a
  plain-text fallback summary built from the raw data if the API call fails.
Do not hardcode the API key — read ANTHROPIC_API_KEY from the environment.
```

### Step 10 — Standup Reporter sub-agent

```
Create subagents/reporter.py with run(client, config) that:
1. Loads all open + recently-closed issues in the sprint milestone.
2. Builds snapshots and infers status for each.
3. Calls llm.generate_standup_summary().
4. Posts the result as a comment on the configured standup_issue_number, prefixed
   with a heading like "## Daily Standup — <today's date>".
Add logging. If there are zero active stories, post a short "nothing in progress"
note instead of calling the LLM (saves tokens).
```

### Step 11 — Event-driven sub-agents

```
Create subagents/pr_watcher.py with run(client, config, event_payload) that reads a
GitHub Actions pull_request event payload, extracts the PR number and the issue it
references (parse "#<n>" from the PR body/title), and posts a comment on that issue
noting a PR is now open for review. Also add a function check_stale_prs(client, config)
that finds open PRs with no review activity beyond a threshold and pings reviewers.

Then create subagents/completion.py with run(client, config, event_payload) that reads
an issues "closed" event, checks whether the closed issue has a linked PR and the
required completion labels from config, and if not, posts a comment flagging that the
story was closed without meeting completion criteria.

For both, explain how to read the event payload from the GITHUB_EVENT_PATH file that
Actions provides.
```

### Step 12 — Orchestrator + workflows

```
Create orchestrator.py with a main() that:
- Reads an argument or env var AGENT_MODE (one of: staleness, standup, pr_watcher,
  completion).
- Loads config and builds the shared GitHubClient.
- Dispatches to the matching sub-agent's run() function.
- For event-driven modes, loads the event payload from GITHUB_EVENT_PATH.
Then generate four GitHub Actions workflow files:
- staleness.yml: cron daily at 9am UTC weekdays, runs AGENT_MODE=staleness.
- standup.yml: cron daily at 9:15am UTC weekdays, runs AGENT_MODE=standup.
- pr-watcher.yml: on pull_request [opened, ready_for_review], runs AGENT_MODE=pr_watcher.
- completion.yml: on issues [closed], runs AGENT_MODE=completion.
Each workflow checks out the repo, sets up Python 3.11, installs requirements, runs
the agent, and (for staleness) commits the updated agent-state.json back to the repo.
Set permissions: issues: write and contents: write. Pass GITHUB_TOKEN and
ANTHROPIC_API_KEY from secrets.
```

---

## Part 6 — Config & Convention Setup

### 6.1 `config.yml` starter

```yaml
repo_name: "your-org/your-repo"
staleness_days: 2
business_days_only: true
sprint_milestone: "Sprint 1"
standup_issue_number: 1          # create one issue titled "Daily Standup" first
completion_labels:               # labels a story must have to count as properly done
  - "status: done"
slack_webhook_url: ""            # leave empty for POC
```

### 6.2 Label scheme to create in the repo

Set these up before running the agent (Settings → Labels, or via the API):

- Type: `epic`, `story`, `subtask`
- Status: `status: in-progress`, `status: blocked`, `status: done`
- Agent: `reminder-sent` (the agent manages this one)

### 6.3 Branch / commit conventions to enforce on the team

The whole staleness mechanism depends on commits referencing issues. Agree on this with the team **before** building:

- Branch names: `feature/<issue-number>-short-desc` (e.g. `feature/42-login-page`)
- Commit messages: include `#<issue-number>` (e.g. `add form validation #42`)
- PR body: include `Closes #<issue-number>`

Without this discipline, the agent can't link activity to stories. This is an operational dependency, not a code problem — flag it to your manager.

---

## Part 7 — Secrets Setup

In the GitHub repo → Settings → Secrets and variables → Actions:

- `ANTHROPIC_API_KEY` — your Claude API key (add as a repo secret).
- `GITHUB_TOKEN` — **automatically provided** by Actions; you don't create this. But you must grant it write permissions in each workflow YAML:

```yaml
permissions:
  issues: write
  contents: write   # needed so staleness.yml can commit agent-state.json
```

For local testing, put both in a `.env` file (never commit it — add to `.gitignore`).

---

## Part 8 — Testing Strategy

Write tests for the pure-logic functions first — they don't need GitHub access:

- `test_metrics.py` — feed `infer_status()` hand-built Story + ActivitySnapshot objects, assert the right StoryStatus comes out. Cover every branch (no assignee, stale, fresh, has PR, closed).
- `test_state.py` — assert `should_remind()` returns False right after `record_reminder()`, and True after the staleness window passes. Use a fixed `now` you control.
- `test_staleness.py` — mock the GitHubClient (use `unittest.mock`) so no real API calls happen; assert the right issues get reminders.

**Prompt for Copilot to generate tests:**

```
Write pytest tests for infer_status() in metrics.py. Create Story and
ActivitySnapshot fixtures covering: a closed issue (expect DONE), an open issue with
a linked PR (expect IN_REVIEW), an open assigned issue with activity 1 day ago and
staleness_days=2 (expect IN_PROGRESS), an open assigned issue with activity 5 days ago
(expect STALLED), and an open unassigned issue (expect NOT_STARTED). Use a fixed
datetime for "now" so tests are deterministic.
```

---

## Part 9 — Local Dev Loop

Before relying on Actions, run everything locally:

```bash
# one-time setup
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # then fill in your tokens

# run a sub-agent manually
AGENT_MODE=staleness python -m agent.orchestrator
AGENT_MODE=standup   python -m agent.orchestrator

# run tests
pytest -v
```

Use a **sandbox repo** with dummy issues for testing — don't point the agent at a real team's repo until the logic is proven. Create 5-6 fake issues with different states (some assigned with recent commits, some assigned and stale, some unassigned) so you can see every code path fire.

---

## Part 10 — Things That Will Bite You (Read Before Building)

1. **PRs show up as issues.** PyGitHub's `get_issues()` returns pull requests too. Always filter `issue.pull_request is None`. (Handled in Step 2.)

2. **Commit-to-issue linking is inferred, not native.** There's no API for "commits on issue #42." If the team doesn't reference issue numbers in commits/branches, the agent is blind. This is the single biggest dependency.

3. **Rate limits (5,000 req/hr).** Naively fetching commits + PRs + comments per issue across a big repo will exhaust this fast. For the POC with a small sandbox it's fine; note in code where caching/GraphQL goes for scale.

4. **`GITHUB_TOKEN` write permissions.** Default is read-mostly. Comments and committing state both fail silently without explicit `permissions:` in the workflow.

5. **State must persist between runs.** The staleness agent commits `agent-state.json` back to the repo at the end of each run. Without `contents: write` permission and the commit step, the agent forgets it already reminded someone and re-spams daily.

6. **Closed ≠ Done.** People close duplicates and rejects. Use the `status: done` label as the real "done" signal, not just closed state. (Handled in Step 11's completion checker.)

7. **Token cost discipline.** The multi-agent split exists partly to limit Claude calls. Only the Standup Reporter calls the LLM, and it skips the call entirely when nothing is active. Don't add LLM calls to the staleness/PR/completion agents — they're pure logic.

---

## Part 11 — Definition of Done for the POC

You can demo the POC when:

- [ ] A stale, assigned story automatically gets an @mention reminder.
- [ ] Running the staleness agent twice in a day does **not** double-remind.
- [ ] A real natural-language standup digest gets posted to the standup issue.
- [ ] Opening a PR that references an issue triggers a comment on that issue.
- [ ] Closing an issue without the `status: done` label triggers a flag comment.
- [ ] All four workflows run automatically (verify with `workflow_dispatch` manual triggers first).
- [ ] Logic functions have passing pytest tests.

---

## Suggested First Session

1. Get steps 1–2 working (auth + read issues) against a sandbox repo. This proves your whole setup before you write any real logic.
2. Then steps 3–5 (models + metrics) — this is the brain, and it's all testable without posting anything.
3. Stop and demo "the agent can correctly classify every story's status." That alone is a strong checkpoint to show your manager.

From there, steps 6–10 add memory and actions, and 11–12 add automation. Don't rush to Actions — a script you run by hand that does the right thing is worth more than an automated one whose logic you haven't validated.
