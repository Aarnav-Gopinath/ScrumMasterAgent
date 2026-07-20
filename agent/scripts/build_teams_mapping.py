"""Build teams_users.yml by resolving UST-PACE org members' GitHub logins to emails.

Usage (from repo root):
    set -a && source .env && set +a && python -m agent.scripts.build_teams_mapping

Output: teams_users.yml in the repo root.

GitHub only exposes a user's email if they have set it as public in their profile.
Members who have no public email are listed under `unresolved` in the output.
"""

from __future__ import annotations

import logging
import os
import sys

import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("build_teams_mapping")

OUTPUT_PATH = "teams_users.yml"


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        logger.error("GITHUB_TOKEN is required.")
        return 1

    from agent.services.config import load_config

    config = load_config("config.yml")
    org_name = config.org_name
    logger.info("Resolving org members for %s …", org_name)

    try:
        from github import Auth, Github

        g = Github(auth=Auth.Token(token))
        org = g.get_organization(org_name)
    except Exception as exc:
        logger.error("Failed to connect to GitHub: %s", exc)
        return 1

    resolved: dict[str, str] = {}   # login → email
    unresolved: list[str] = []

    try:
        members = list(org.get_members())
    except Exception as exc:
        logger.error("Failed to list org members: %s", exc)
        return 1

    logger.info("Found %d org member(s). Resolving emails …", len(members))
    for member in members:
        login = member.login
        try:
            user = g.get_user(login)
            email = user.email
        except Exception as exc:  # noqa: BLE001 — keep going on per-user failures
            logger.warning("Could not fetch profile for %s: %s", login, exc)
            unresolved.append(login)
            continue

        if email:
            resolved[login] = email
            logger.info("  ✓ %s → %s", login, email)
        else:
            unresolved.append(login)
            logger.info("  ✗ %s — no public email", login)

    mapping = {
        "users": {login: {"email": email} for login, email in sorted(resolved.items())},
        "unresolved": sorted(unresolved),
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as fh:
        yaml.dump(mapping, fh, default_flow_style=False, allow_unicode=True)

    print(f"\n{'='*60}")
    print(f"teams_users.yml written to {OUTPUT_PATH}")
    print(f"  Resolved:   {len(resolved)} member(s)")
    print(f"  Unresolved: {len(unresolved)} member(s)")
    if unresolved:
        print(f"\nUnresolved logins:")
        for login in sorted(unresolved):
            print(f"  - {login}")
    print("="*60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
