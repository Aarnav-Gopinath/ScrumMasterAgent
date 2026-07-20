"""Teams webhook routing and posting helpers."""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)


class TeamsNotifier:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    @classmethod
    def for_repo(cls, repo_full_name: str, config) -> "TeamsNotifier | None":
        repo_name = (repo_full_name or "").lower()
        for team in config.teams:
            repos = [str(name).lower() for name in (team.get("repos") or [])]
            if repo_name in repos:
                return cls(team.get("webhook_url") or "")

        if config.teams_fallback_webhook:
            return cls(config.teams_fallback_webhook)
        return None

    def post(self, text: str) -> None:
        payload = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": [{"type": "TextBlock", "text": text, "wrap": True}],
                    },
                }
            ],
        }

        try:
            response = requests.post(self.webhook_url, json=payload, timeout=15)
            logger.info("Teams webhook response status: %s", response.status_code)
        except Exception as exc:  # noqa: BLE001 - Teams failures must not break reporter.
            logger.exception("Teams webhook post failed: %s", exc)
            return

    def post_if_configured(self, text: str) -> None:
        if not self.webhook_url:
            logger.debug("Teams webhook skipped: empty webhook_url")
            return
        self.post(text)
