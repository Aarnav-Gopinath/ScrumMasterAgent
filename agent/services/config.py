"""Load `config.yml` into a typed, immutable Config dataclass.

Keeping config in a committed YAML file (rather than env vars or a database) means
thresholds like `staleness_days` can be tuned without code changes, and the values
are version-controlled alongside the agent. For the POC this is plenty; nothing here
needs a hosted config service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os

import yaml


@dataclass(frozen=True)
class Config:
    """Typed view over `config.yml`.

    Fields mirror the YAML keys. Defaults match the build plan so a partial config
    file still produces a usable object.
    """

    org_name: str
    agent_repo: str
    standup_issue_number: int
    staleness_days: int = 2
    abandoned_days: int = 30
    business_days_only: bool = True
    completion_labels: list[str] = field(default_factory=list)
    slack_webhook_url: str = ""
    jira_base_url: str = ""


def load_config(path: str = "config.yml") -> Config:
    """Read `path` and return a Config.

    Raises FileNotFoundError if the file is missing and KeyError if a required field
    (org_name, agent_repo, standup_issue_number) is absent — failing loudly is better
    than running the agent against an unconfigured org.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    return Config(
        org_name=raw["org_name"],
        agent_repo=raw["agent_repo"],
        standup_issue_number=int(raw["standup_issue_number"]),
        staleness_days=int(raw.get("staleness_days", 2)),
        abandoned_days=int(raw.get("abandoned_days", 30)),
        business_days_only=bool(raw.get("business_days_only", True)),
        completion_labels=list(raw.get("completion_labels") or []),
        slack_webhook_url=raw.get("slack_webhook_url") or "",
        jira_base_url=raw.get("jira_base_url") or os.environ.get("JIRA_BASE_URL", ""),
    )
