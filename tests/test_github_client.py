"""Tests for GitHubClient helper methods over FixtureRepo."""

from __future__ import annotations


def test_get_branch_committers_deduplicates_across_sources(client):
    committers = client.get_branch_committers(client.repo, 42)
    assert committers == ["alice", "dave", "bob"]


def test_get_branch_committers_empty_when_no_matches(client):
    committers = client.get_branch_committers(client.repo, 777)
    assert committers == []
