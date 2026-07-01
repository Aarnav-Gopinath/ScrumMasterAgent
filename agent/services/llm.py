"""Claude integration — the only place the agent spends LLM tokens.

`generate_standup_summary` turns structured story data into a natural-language standup
report. By design this is the *sole* LLM call in the whole agent (the staleness / PR /
completion sub-agents are pure logic), which keeps token cost predictable.

The API key is read from ANTHROPIC_API_KEY at call time, never hardcoded. If the SDK
is missing, the key is unset, or the API errors, we fall back to a plain-text summary
built from the raw data so the standup still posts something useful.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Iterable

from agent.models import ActivitySnapshot, Story, StoryStatus

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1024

SYSTEM_PROMPT = (
    "You are a concise, upbeat scrum master summarizing a software sprint's daily "
    "status for the team. Be specific and brief. Do not invent information that isn't "
    "in the data. Format the report as GitHub-flavored Markdown with these sections:\n"
    "1. A 2-3 sentence overview of where the sprint stands.\n"
    "2. A short per-developer breakdown of what each person has in progress, stalled, "
    "or in review (skip developers with nothing active).\n"
    "3. A '**Needs attention**' section that explicitly lists every stalled story by "
    "number and title. If nothing is stalled, say so in one line."
)


def _story_payload(
    stories_with_status: Iterable[tuple[Story, StoryStatus, ActivitySnapshot]],
) -> list[dict]:
    """Flatten (Story, StoryStatus, ActivitySnapshot) tuples into compact JSON-able
    dicts for the prompt. Only the fields Claude needs are included, to save tokens."""
    payload = []
    for story, status, snapshot in stories_with_status:
        last = snapshot.last_activity_at
        payload.append(
            {
                "number": story.number,
                "title": story.title,
                "status": status.value,
                "assignees": story.assignees,
                "last_activity": last.date().isoformat() if last else None,
                "commits": snapshot.commit_count,
                "prs": snapshot.pr_count,
                "comments": snapshot.comment_count,
            }
        )
    return payload


def _fallback_summary(payload: list[dict]) -> str:
    """Deterministic plain-text digest used when the LLM is unavailable.

    Groups stories by status so the report is still readable and the stalled items are
    still surfaced — the standup must never silently produce nothing.
    """
    by_status: dict[str, list[dict]] = {}
    for item in payload:
        by_status.setdefault(item["status"], []).append(item)

    lines = ["_(LLM unavailable — auto-generated summary from raw data.)_", ""]
    for status in (
        StoryStatus.IN_PROGRESS,
        StoryStatus.IN_REVIEW,
        StoryStatus.STALLED,
        StoryStatus.NOT_STARTED,
        StoryStatus.DONE,
    ):
        items = by_status.get(status.value, [])
        if not items:
            continue
        label = status.value.replace("_", " ").title()
        lines.append(f"**{label}** ({len(items)}):")
        for item in items:
            who = ", ".join(item["assignees"]) or "unassigned"
            lines.append(f"- #{item['number']} {item['title']} — {who}")
        lines.append("")

    stalled = by_status.get(StoryStatus.STALLED.value, [])
    lines.append("**Needs attention**:")
    if stalled:
        for item in stalled:
            lines.append(f"- #{item['number']} {item['title']} is stalled.")
    else:
        lines.append("- Nothing stalled. 🎉")
    return "\n".join(lines).strip()


def generate_standup_summary(
    stories_with_status: Iterable[tuple[Story, StoryStatus, ActivitySnapshot]],
) -> str:
    """Return a natural-language standup report as a Markdown string.

    Sends the story data to Claude; on any failure (missing SDK, missing key, API
    error) returns `_fallback_summary` instead of raising, so the caller can always
    post *something*.
    """
    payload = _story_payload(stories_with_status)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set — using fallback summary.")
        return _fallback_summary(payload)

    try:
        # Imported lazily so the rest of the agent (and the offline demo/tests) never
        # need the anthropic SDK installed just to import this module.
        from anthropic import Anthropic

        client = Anthropic(api_key=api_key)
        message = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Here is today's sprint story data as JSON. Write the standup "
                        "report.\n\n```json\n"
                        + json.dumps(payload, indent=2)
                        + "\n```"
                    ),
                }
            ],
        )
        # Concatenate any text blocks in the response.
        text = "".join(
            block.text for block in message.content if getattr(block, "type", None) == "text"
        ).strip()
        return text or _fallback_summary(payload)
    except Exception as exc:  # noqa: BLE001 — any SDK/API failure falls back gracefully.
        logger.exception("Claude call failed (%s) — using fallback summary.", exc)
        return _fallback_summary(payload)
