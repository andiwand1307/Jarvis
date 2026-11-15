"""Turns the add-on's /data/options.json into shell `export` lines (used by run.sh)."""

from __future__ import annotations

import json
import shlex
import sys

OPTION_ENV = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "model": "JARVIS_MODEL",
    "effort": "JARVIS_EFFORT",
    "user_name": "JARVIS_USER_NAME",
    "web_search": "JARVIS_WEB_SEARCH",
    "todoist_api_token": "TODOIST_API_TOKEN",
    "mail_imap_host": "MAIL_IMAP_HOST",
    "mail_imap_port": "MAIL_IMAP_PORT",
    "mail_smtp_host": "MAIL_SMTP_HOST",
    "mail_smtp_port": "MAIL_SMTP_PORT",
    "mail_smtp_security": "MAIL_SMTP_SECURITY",
    "mail_user": "MAIL_USER",
    "mail_password": "MAIL_PASSWORD",
    "mail_from": "MAIL_FROM",
    "mail_drafts_folder": "MAIL_DRAFTS_FOLDER",
    "ha_trusted_entities": "HA_TRUSTED_ENTITIES",
}


def export_lines(options: dict) -> list[str]:
    lines = []
    for key, env in OPTION_ENV.items():
        value = options.get(key)
        if isinstance(value, list):
            value = ",".join(str(v).strip() for v in value if str(v).strip())
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        lines.append(f"export {env}={shlex.quote(str(value))}")
    return lines


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else "/data/options.json"
    try:
        with open(path, encoding="utf-8") as fh:
            options = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"echo {shlex.quote(f'options.json nicht lesbar: {exc}')} >&2")
        return 0
    print("\n".join(export_lines(options)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
