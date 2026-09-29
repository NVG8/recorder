"""One place that knows about every Google OAuth token these projects use.

Token files tend to drift across projects, with overlapping scopes and no way
to tell a live token from a dead one. A refresh token can fail silently for
months before anything surfaces it. That is the failure this module exists to
prevent.

Deliberately a *registry*, not a migration: the token files stay exactly where
they are, because other tools on the same machine may read them from fixed
paths. What changes is that one module knows the full set, so it can be checked,
refreshed, and re-authorized as a unit.

  uv run python google_auth.py status          # what's alive, what's dead
  uv run python google_auth.py status --deep   # force a refresh to be sure
  uv run python google_auth.py status --json   # machine-readable; always exits 0
  uv run python google_auth.py reauth drive    # fix one
  uv run python google_auth.py reauth --all    # fix everything that's broken

As a library:

    from google_auth import service_for
    drive = service_for("drive", "drive", "v3")   # None if unusable
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import recorder_config as rc

# The OAuth client all these tokens were minted against. Create a Desktop OAuth
# client in Google Cloud Console and save its JSON here.
DEFAULT_CLIENT_SECRETS = rc.CLIENT_SECRETS

CAL_RO = "https://www.googleapis.com/auth/calendar.readonly"
GMAIL_RO = "https://www.googleapis.com/auth/gmail.readonly"
DRIVE_RO = "https://www.googleapis.com/auth/drive.readonly"
DOCS_RO = "https://www.googleapis.com/auth/documents.readonly"


@dataclass
class TokenSpec:
    name: str
    path: Path
    scopes: list[str]
    purpose: str
    # Set False for a token another tool mints through its own flow;
    # re-authorizing it here would overwrite it with a differently-scoped one.
    reauthable: bool = True
    client_secrets: Optional[Path] = None
    used_by: list[str] = field(default_factory=list)


TOKENS: dict[str, TokenSpec] = {
    "calendar": TokenSpec(
        name="calendar",
        path=rc.CONFIG_DIR / "token.json",
        scopes=[CAL_RO],
        purpose="Read calendar events for meeting prep",
        used_by=["recorder (prep)"],
    ),
    f"gmail_{rc.DEFAULT_ACCOUNT}": TokenSpec(
        name=f"gmail_{rc.DEFAULT_ACCOUNT}",
        path=rc.CONFIG_DIR / f"gmail_token_{rc.DEFAULT_ACCOUNT}.json",
        scopes=[GMAIL_RO],
        purpose="Email history + Gemini Notes (EMAIL_SEARCH_BACKEND=gmail)",
        used_by=["recorder (prep, import)"],
    ),
    "drive": TokenSpec(
        name="drive",
        path=rc.CONFIG_DIR / "drive_token.json",
        scopes=[DRIVE_RO, DOCS_RO],
        purpose="Read Meet/Gemini notes docs attached to calendar events",
        used_by=["recorder (import_meet_notes)"],
    ),
    "match_calendar": TokenSpec(
        name="match_calendar",
        path=rc.MATCH_CALENDAR_TOKEN,
        scopes=[CAL_RO],
        purpose="Multi-calendar lookup for recording→meeting matching",
        used_by=["recorder (fetch_meeting)"],
    ),
}


def _load(spec: TokenSpec) -> Optional[Credentials]:
    if not spec.path.exists():
        return None
    try:
        return Credentials.from_authorized_user_file(str(spec.path), spec.scopes)
    except Exception:  # noqa: BLE001
        return None


def check(spec: TokenSpec, deep: bool = False) -> dict[str, Any]:
    """Report one token's health.

    `deep` forces a refresh even when the cached access token still looks valid.
    Access tokens live about an hour, so a shallow check can call a token with a
    *revoked* refresh token healthy. Use deep when the answer matters.
    """
    result: dict[str, Any] = {
        "name": spec.name,
        "path": str(spec.path),
        "purpose": spec.purpose,
        "usedBy": spec.used_by,
        "exists": spec.path.exists(),
        "status": "missing",
        "detail": "",
    }
    if not spec.path.exists():
        result["detail"] = "token file not found"
        return result

    creds = _load(spec)
    if creds is None:
        result["status"] = "unreadable"
        result["detail"] = "could not parse token file"
        return result

    result["scopes"] = [s.split("/auth/")[-1] for s in (creds.scopes or [])]
    needs_refresh = deep or not creds.valid
    if not needs_refresh:
        result["status"] = "ok"
        result["detail"] = "access token still valid (not deep-checked)"
        return result

    if not creds.refresh_token:
        result["status"] = "dead"
        result["detail"] = "no refresh token; re-auth required"
        return result

    try:
        creds.refresh(Request())
    except Exception as exc:  # noqa: BLE001
        result["status"] = "dead"
        result["detail"] = f"{type(exc).__name__}: {exc}"
        return result

    try:
        rc.write_secret(spec.path, creds.to_json())
    except OSError as exc:
        result["detail"] = f"refreshed but could not persist: {exc}"
    result["status"] = "ok"
    result["detail"] = result["detail"] or "refreshed"
    return result


def status(deep: bool = False) -> list[dict[str, Any]]:
    return [check(spec, deep=deep) for spec in TOKENS.values()]


def credentials(name: str) -> Optional[Credentials]:
    """Usable credentials for a registry entry, or None with a clear reason."""
    spec = TOKENS.get(name)
    if spec is None:
        print(f"unknown token {name!r}; known: {', '.join(TOKENS)}", file=sys.stderr)
        return None
    creds = _load(spec)
    if creds is None:
        print(f"{name}: token missing or unreadable at {spec.path}", file=sys.stderr)
        return None
    if creds.valid:
        return creds
    if not creds.refresh_token:
        print(f"{name}: no refresh token — run: google_auth.py reauth {name}", file=sys.stderr)
        return None
    try:
        creds.refresh(Request())
    except Exception as exc:  # noqa: BLE001
        print(
            f"{name}: refresh failed ({type(exc).__name__}: {exc}) — "
            f"run: google_auth.py reauth {name}",
            file=sys.stderr,
        )
        return None
    try:
        rc.write_secret(spec.path, creds.to_json())
    except OSError:
        pass
    return creds


def service_for(name: str, api: str, version: str) -> Any:
    """Build a Google API client from a registry entry, or None."""
    creds = credentials(name)
    if creds is None:
        return None
    try:
        return build(api, version, credentials=creds, cache_discovery=False)
    except Exception as exc:  # noqa: BLE001
        print(f"{name}: building {api} client failed: {exc}", file=sys.stderr)
        return None


def reauth(name: str) -> bool:
    """Run the interactive consent flow and write a fresh token."""
    spec = TOKENS.get(name)
    if spec is None:
        print(f"unknown token {name!r}", file=sys.stderr)
        return False
    if not spec.reauthable:
        print(
            f"{name} is owned by another project's auth flow; re-authorize it there",
            file=sys.stderr,
        )
        return False
    secrets = spec.client_secrets or DEFAULT_CLIENT_SECRETS
    if not secrets.exists():
        print(f"OAuth client not found: {secrets}", file=sys.stderr)
        return False
    print(f"\n→ {name}: {spec.purpose}")
    print(f"  scopes: {', '.join(s.split('/auth/')[-1] for s in spec.scopes)}")
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), spec.scopes)
    creds = flow.run_local_server(port=0)
    spec.path.parent.mkdir(parents=True, exist_ok=True)
    rc.write_secret(spec.path, creds.to_json())
    print(f"  ✓ wrote {spec.path}")
    return True


_SYMBOL = {"ok": "✓", "dead": "✗", "missing": "?", "unreadable": "?"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Report health of every token")
    p_status.add_argument(
        "--deep",
        action="store_true",
        help="Force a refresh — a cached access token can mask a revoked refresh token",
    )
    p_status.add_argument("--json", action="store_true")

    p_reauth = sub.add_parser("reauth", help="Re-authorize one or more tokens")
    p_reauth.add_argument("name", nargs="?", help="Registry name, e.g. drive")
    p_reauth.add_argument(
        "--all", action="store_true", help="Re-authorize every token that isn't ok"
    )

    args = parser.parse_args()

    if args.command == "status":
        rows = status(deep=args.deep)
        if args.json:
            json.dump(rows, sys.stdout, indent=2)
            sys.stdout.write("\n")
            # Machine consumers read health from the payload, so this always
            # exits 0. Signalling failure via the exit code as well would make
            # the app's subprocess wrapper throw exactly when a token is dead —
            # i.e. lose the report precisely when there is something to report.
            return 0
        else:
            for row in rows:
                mark = _SYMBOL.get(row["status"], "?")
                print(f"{mark} {row['name']:<20} {row['status']:<11} {row['detail'][:60]}")
                print(f"  {row['purpose']}")
                print(f"  used by: {', '.join(row['usedBy'])}")
                print(f"  {row['path']}")
                print()
            broken = [r["name"] for r in rows if r["status"] != "ok"]
            if broken:
                print(f"needs attention: {', '.join(broken)}")
                print("fix with: uv run python google_auth.py reauth --all")
        return 1 if any(r["status"] != "ok" for r in rows) else 0

    if args.all:
        rows = status(deep=True)
        targets = [
            r["name"]
            for r in rows
            if r["status"] != "ok" and TOKENS[r["name"]].reauthable
        ]
        if not targets:
            print("nothing to re-authorize")
            return 0
        print(f"re-authorizing: {', '.join(targets)}")
        return 0 if all(reauth(n) for n in targets) else 1

    if not args.name:
        print("give a token name or --all", file=sys.stderr)
        return 2
    return 0 if reauth(args.name) else 1


if __name__ == "__main__":
    raise SystemExit(main())
