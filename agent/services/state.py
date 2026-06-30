"""The agent's memory: a JSON file keyed by issue number.

For the POC, state lives in a committed file (`agent-state.json`) that the staleness
workflow commits back to the repo after each run — zero external infrastructure. The
trade-off is that every run rewrites the whole file and concurrent runs could race;
for production you'd swap this module's four functions for a small SQLite or Postgres
table (the public interface would stay the same).

Each entry holds:
  last_reminder_sent_at : ISO-8601 string or None
  reminder_count        : int
  last_status           : str (the StoryStatus value last seen)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Optional


def load_state(path: str) -> dict:
    """Return the state dict, or an empty dict if the file doesn't exist yet."""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_state(path: str, state: dict) -> None:
    """Write state back to `path` as pretty JSON (stable key order for clean diffs)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)


def _entry(state: dict, issue_number: int) -> Optional[dict]:
    # JSON object keys are always strings; normalize lookups through str().
    return state.get(str(issue_number))


def should_remind(
    state: dict, issue_number: int, staleness_days: int, now: datetime
) -> bool:
    """True only if we have NOT reminded about this issue within the last
    `staleness_days`. This is what makes the staleness agent idempotent within a day —
    running it twice won't double-ping."""
    entry = _entry(state, issue_number)
    if entry is None:
        return True
    last_raw = entry.get("last_reminder_sent_at")
    if not last_raw:
        return True
    last = datetime.fromisoformat(last_raw)
    return (now - last) >= timedelta(days=staleness_days)


def record_reminder(
    state: dict, issue_number: int, now: datetime, last_status: Optional[str] = None
) -> None:
    """Stamp the reminder time and bump the count for this issue (mutates `state`)."""
    key = str(issue_number)
    entry = state.get(key, {"last_reminder_sent_at": None, "reminder_count": 0, "last_status": None})
    entry["last_reminder_sent_at"] = now.isoformat()
    entry["reminder_count"] = entry.get("reminder_count", 0) + 1
    if last_status is not None:
        entry["last_status"] = last_status
    state[key] = entry
