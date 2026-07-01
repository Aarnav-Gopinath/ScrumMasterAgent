"""Entry point: routes a run to the right sub-agent based on AGENT_MODE.

GitHub Actions invokes this with `AGENT_MODE` set (staleness | standup | pr_watcher |
completion). Cron modes (staleness, standup) run on a schedule; event modes
(pr_watcher, completion) read the triggering event from the JSON file GitHub Actions
writes to GITHUB_EVENT_PATH.

Run locally with, e.g.:  AGENT_MODE=staleness python -m agent.orchestrator
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

from agent.services.config import load_config
from agent.services.github_client import GitHubClient
from agent.services.state import load_state
from agent.subagents import completion, pr_watcher, reporter, staleness

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("orchestrator")

STATE_PATH = "agent-state.json"


def load_event() -> dict:
    """Read and parse the event payload GitHub Actions drops at GITHUB_EVENT_PATH.

    Returns an empty dict if the var is unset or the file is missing (e.g. local runs),
    so the event sub-agents degrade to "nothing to do" rather than crashing.
    """
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        logger.warning("GITHUB_EVENT_PATH not available — using empty event payload.")
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    mode = os.environ.get("AGENT_MODE")
    if len(sys.argv) > 1:
        mode = sys.argv[1]  # allow `python -m agent.orchestrator staleness` too

    valid = {"staleness", "standup", "pr_watcher", "completion"}
    if mode not in valid:
        logger.error("AGENT_MODE must be one of %s (got %r).", sorted(valid), mode)
        return 2

    config = load_config()
    client = GitHubClient.from_token(config.repo_name)
    now = datetime.now(timezone.utc)
    logger.info("Running mode=%s against %s", mode, config.repo_name)

    if mode == "staleness":
        state = load_state(STATE_PATH)
        summary = staleness.run(client, config, state, now, state_path=STATE_PATH)
        logger.info("Staleness summary: %s", summary)

    elif mode == "standup":
        reporter.run(client, config, now)

    elif mode == "pr_watcher":
        event = load_event()
        summary = pr_watcher.run(client, config, event)
        logger.info("PR watcher summary: %s", summary)

    elif mode == "completion":
        event = load_event()
        result = completion.run(client, config, event)
        logger.info("Completion result: %s", result)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
