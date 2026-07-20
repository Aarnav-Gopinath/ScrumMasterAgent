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
from datetime import datetime, timedelta, timezone
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


# ----- repo activity cache ---------------------------------------------------


def get_cached_repo_activity(
    state: dict,
    repo_full_name: str,
    cache_ttl_hours: int = 6,
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    """Return cached last_activity for `repo_full_name` if the entry is still fresh.

    Returns None on cache miss (not found) or cache expiry (cached_at older than
    `cache_ttl_hours`). The caller should then fetch from the API and call
    `cache_repo_activity` to populate the cache.

    `now` can be injected for deterministic tests; otherwise real wall-clock is used.
    """
    cache = state.get("repo_cache", {})
    entry = cache.get(repo_full_name)
    if entry is None:
        return None

    cached_at_raw = entry.get("cached_at")
    if not cached_at_raw:
        return None

    cached_at = datetime.fromisoformat(cached_at_raw)
    check_time = now if now is not None else datetime.now(cached_at.tzinfo or timezone.utc)
    age_hours = (check_time - cached_at).total_seconds() / 3600
    if age_hours > cache_ttl_hours:
        return None

    last_activity_raw = entry.get("last_activity")
    if last_activity_raw is None:
        # Cached value is "no activity" — this is a valid hit, but callers can't
        # distinguish it from a miss via the return value. The presence of the
        # entry in the cache indicates a hit; callers that need this distinction
        # should inspect state["repo_cache"] directly.
        return None
    return datetime.fromisoformat(last_activity_raw)


def cache_repo_activity(
    state: dict,
    repo_full_name: str,
    last_activity: Optional[datetime],
    now: datetime,
) -> None:
    """Store `last_activity` for `repo_full_name` in the state cache (mutates `state`)."""
    if "repo_cache" not in state:
        state["repo_cache"] = {}
    state["repo_cache"][repo_full_name] = {
        "last_activity": last_activity.isoformat() if last_activity is not None else None,
        "cached_at": now.isoformat(),
    }
