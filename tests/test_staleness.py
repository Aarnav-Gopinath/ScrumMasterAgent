"""Tests for the Staleness Monitor sub-agent."""

from __future__ import annotations

from datetime import timedelta

from agent.services.config import load_config
from agent.subagents import staleness
from tests.conftest import NOW


def _config():
    return load_config("config.yml")


class _SpyNotifier:
    def __init__(self):
        self.committer_calls: list[tuple[int, list[str], int]] = []
        self.assignee_calls: list[tuple[int, int]] = []

    def ask_committers_for_status(self, repo, story, committers, days_stale):
        _ = repo
        self.committer_calls.append((story.number, list(committers), days_stale))

    def remind_assignee(self, story, days_stale, repo=None):
        _ = repo
        self.assignee_calls.append((story.number, days_stale))


def test_stalled_story_prefers_branch_committers(client):
    notifier = _SpyNotifier()
    state: dict = {}
    summary = staleness.run(client, _config(), state, NOW, notifier=notifier)

    reminded = [num for num, action in summary if action.startswith("reminded")]
    assert reminded == [42]
    assert notifier.assignee_calls == []
    assert notifier.committer_calls == [(42, ["alice", "dave", "bob"], 2)]


def test_falls_back_to_assignee_when_no_branch_committers(client, monkeypatch):
    notifier = _SpyNotifier()
    state: dict = {}
    monkeypatch.setattr(client, "get_branch_committers", lambda repo, issue_number: [])

    summary = staleness.run(client, _config(), state, NOW, notifier=notifier)

    reminded = [num for num, action in summary if action.startswith("reminded")]
    assert reminded == [42]
    assert notifier.committer_calls == []
    assert notifier.assignee_calls == [(42, 2)]


def test_running_twice_does_not_double_remind(client):
    config = _config()
    notifier = _SpyNotifier()
    state: dict = {}

    staleness.run(client, config, state, NOW, notifier=notifier)
    summary2 = staleness.run(client, config, state, NOW, notifier=notifier)

    actions = dict((str(n), a) for n, a in summary2)
    assert actions["42"] == "skip:already-reminded"
    assert len(notifier.committer_calls) == 1
    assert len(notifier.assignee_calls) == 0


def test_reminds_again_after_window(client):
    config = _config()
    notifier = _SpyNotifier()
    state: dict = {}

    staleness.run(client, config, state, NOW, notifier=notifier)
    later = NOW + timedelta(days=config.staleness_days + 1)
    summary = staleness.run(client, config, state, later, notifier=notifier)

    actions = dict((n, a) for n, a in summary)
    assert actions[42].startswith("reminded")
    calls_on_42 = [call for call in notifier.committer_calls if call[0] == 42]
    assert len(calls_on_42) == 2
