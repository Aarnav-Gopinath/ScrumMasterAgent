"""Tests for the Staleness Monitor sub-agent.

Uses a GitHubClient over the JSON fixtures (no network). In the fixtures, only #3
(assigned to bob, last activity 6 days ago) is STALLED."""

from __future__ import annotations

from datetime import timedelta

from agent.services.config import load_config
from agent.subagents import staleness
from tests.conftest import NOW


def _config():
    return load_config("config.yml")


def test_only_stalled_story_gets_reminded(client):
    state: dict = {}
    summary = staleness.run(client, _config(), state, NOW)

    reminded = [num for num, action in summary if action == "reminded"]
    assert reminded == [3]
    # Exactly one comment, on #3, @mentioning bob.
    posted = client.repo.posted_comments
    assert len(posted) == 1
    assert posted[0]["issue_number"] == 3
    assert "@bob" in posted[0]["body"]


def test_running_twice_does_not_double_remind(client):
    config = _config()
    state: dict = {}

    staleness.run(client, config, state, NOW)
    # Second run on the same day with the same state: no new comments.
    summary2 = staleness.run(client, config, state, NOW)

    assert ("3", ) not in summary2  # sanity
    actions = dict((str(n), a) for n, a in summary2)
    assert actions["3"] == "skip:already-reminded"
    # Still only the single comment from the first run.
    assert len(client.repo.posted_comments) == 1


def test_reminds_again_after_window(client):
    config = _config()
    state: dict = {}

    staleness.run(client, config, state, NOW)
    later = NOW + timedelta(days=config.staleness_days + 1)
    summary = staleness.run(client, config, state, later)

    actions = dict((n, a) for n, a in summary)
    assert actions[3] == "reminded"
    # #3 has now been reminded on both runs (other stories may also age into STALLED,
    # so we count #3's comments specifically rather than the global total).
    comments_on_3 = [c for c in client.repo.posted_comments if c["issue_number"] == 3]
    assert len(comments_on_3) == 2
