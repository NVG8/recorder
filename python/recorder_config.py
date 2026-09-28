"""Who "I" am, which calendars and mailboxes are mine, and where data lives.

Every script in the sidecar reads identity and paths from here, so nothing
personal is hardcoded. Values come from a JSON file (default
`~/.config/recorder/config.json`, override with `RECORDER_CONFIG`), with a few
env-var overrides on top. See `config.example.json` for the shape.

Everything is optional. With no config at all the recorder still records,
transcribes and extracts todos; calendar matching and meeting prep just have
less to work with.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(
    os.environ.get("RECORDER_CONFIG", "~/.config/recorder/config.json")
).expanduser()


def _load() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


_CFG = _load()

USER_NAME: str = os.environ.get("RECORDER_USER_NAME") or _CFG.get("user_name") or "the user"
USER_FIRST_NAME: str = USER_NAME.split()[0] if USER_NAME != "the user" else "the user"
USER_EMAIL: str = os.environ.get("RECORDER_USER_EMAIL") or _CFG.get("user_email") or ""

# Where OAuth tokens, the OAuth client file, daily briefs and prep config live.
DATA_ROOT = Path(
    os.environ.get("RECORDER_DATA_ROOT") or _CFG.get("data_root") or "~/.config/recorder"
).expanduser()
CONFIG_DIR = DATA_ROOT / "config"
BRIEFS_DIR = DATA_ROOT / "briefs"
LOGS_DIR = DATA_ROOT / "logs"
ENV_FILE = DATA_ROOT / ".env"
CLIENT_SECRETS = CONFIG_DIR / "google_oauth_client.json"

RECORDINGS_DIR = Path(
    os.environ.get("RECORDER_RECORDINGS_DIR")
    or "~/Library/Application Support/Recorder/recordings"
).expanduser()

MAIL_DIR = Path(os.environ.get("RECORDER_MAIL_DIR") or _CFG.get("mail_dir") or "~/mail").expanduser()

# Account short-name -> {"emails": [...], "calendar_ids": [...]}. An account is
# one work identity: a mailbox plus the calendar(s) it owns.
_default_accounts = {"work": {"emails": [USER_EMAIL], "calendar_ids": ["primary"]}} if USER_EMAIL else {}
ACCOUNTS: dict[str, dict[str, list[str]]] = _CFG.get("accounts") or _default_accounts

DEFAULT_ACCOUNT: str = (
    os.environ.get("RECORDER_ACCOUNT")
    or _CFG.get("default_account")
    or next(iter(ACCOUNTS), "work")
)


def account_emails(account: str | None) -> list[str]:
    return list((ACCOUNTS.get((account or "").lower()) or {}).get("emails") or [])


def account_calendar_ids(account: str | None) -> list[str]:
    acct = ACCOUNTS.get((account or "").lower()) or {}
    return list(acct.get("calendar_ids") or acct.get("emails") or [])


# Calendars searched when matching a recording to a meeting. Keep personal and
# family calendars out; their long blocks cause false matches.
MATCH_CALENDARS: list[str] = _CFG.get("match_calendars") or [
    c for a in ACCOUNTS.values() for c in (a.get("calendar_ids") or a.get("emails") or [])
] or ["primary"]

# Domains treated as internal (colleagues, not worth prep enrichment).
INTERNAL_DOMAINS: set[str] = {d.lower() for d in _CFG.get("internal_domains") or []}

# Optional: token file for the calendar-matching lookup, if you already have one.
MATCH_CALENDAR_TOKEN = Path(
    os.environ.get("RECORDER_MATCH_TOKEN_PATH")
    or _CFG.get("match_calendar_token")
    or CONFIG_DIR / "calendar_token.json"
).expanduser()


def write_secret(path: Path, text: str) -> None:
    """Write a credential file readable only by the current user (0600).

    `Path.write_text` honors the umask, which on macOS leaves tokens
    world-readable (0644). Refresh tokens deserve better.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(path, 0o600)  # O_CREAT's mode is ignored when the file already exists
