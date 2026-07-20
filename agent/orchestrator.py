"""Entry point: routes a run to the right sub-agent based on AGENT_MODE."""

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

STATE_PATH = "agent-state.json"
VALID_MODES = {"staleness", "standup", "pr_watcher", "completion"}


def _configure_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    return logging.getLogger("orchestrator")


def _load_event_payload(logger: logging.Logger) -> dict:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        logger.error("GITHUB_EVENT_PATH is required for event-driven modes.")
        raise SystemExit(1)
    if not os.path.exists(path):
        logger.error("GITHUB_EVENT_PATH does not exist: %s", path)
        raise SystemExit(1)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError:
        logger.error("GITHUB_EVENT_PATH contains invalid JSON: %s", path)
        raise SystemExit(1)


def main() -> None:
    logger = _configure_logging()

    mode = os.environ.get("AGENT_MODE")
    if mode is None:
        logger.error("AGENT_MODE is required. Valid values: %s", sorted(VALID_MODES))
        raise SystemExit(1)
    if mode not in VALID_MODES:
        logger.error("Unrecognized AGENT_MODE %r. Valid values: %s", mode, sorted(VALID_MODES))
        raise SystemExit(1)

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        logger.error("GITHUB_TOKEN is required.")
        raise SystemExit(1)

    config = load_config("config.yml")
    client = GitHubClient.from_org_token(token, config.org_name)
    now = datetime.now(timezone.utc)
    logger.info(
        "Starting %s run for %s at %s",
        mode,
        config.org_name,
        now.isoformat(),
    )

    try:
        if mode == "staleness":
            state = load_state(STATE_PATH)
            staleness.run(client, config, state, now=now, state_path=STATE_PATH)
        elif mode == "standup":
            reporter.run(client, config, now=now)
        elif mode == "pr_watcher":
            event_payload = _load_event_payload(logger)
            event_repo = event_payload.get("repository", {}).get("full_name")
            if event_repo:
                event_client = GitHubClient.from_token(event_repo, token=token)
            else:
                logger.error("Event payload missing repository.full_name for pr_watcher mode.")
                raise SystemExit(1)
            pr_watcher.run(event_client, config, event_payload)
        elif mode == "completion":
            event_payload = _load_event_payload(logger)
            event_repo = event_payload.get("repository", {}).get("full_name")
            if event_repo:
                event_client = GitHubClient.from_token(event_repo, token=token)
            else:
                logger.error("Event payload missing repository.full_name for completion mode.")
                raise SystemExit(1)
            completion.run(event_client, config, event_payload)
    except SystemExit:
        raise
    except Exception:
        logger.exception("Run failed with an unhandled exception.")
        raise SystemExit(1)

    logger.info("%s run completed successfully", mode)


if __name__ == "__main__":
    main()
