"""Import Gemini Notes emails from notmuch into Recorder's history.

Writes Recorder-compatible files in the recordings directory:
  rec-YYYYMMDD-HHMMSS.transcript.json
  rec-YYYYMMDD-HHMMSS.event.json

No audio files are required; the Swift app already treats transcript/event files
as the source of truth for history.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable


DEFAULT_RECORDINGS_DIR = (
    Path.home() / "Library" / "Application Support" / "Recorder" / "recordings"
)
GEMINI_SENDER = "gemini-notes@google.com"
FOOTER_MARKERS = (
    "Meeting records",
    "Is the Next Steps section",
    "Google LLC,",
    "You have received this email because meeting artifacts",
)


def flatten_messages(node: Any) -> Iterable[dict[str, Any]]:
    if isinstance(node, dict) and "headers" in node:
        yield node
    if isinstance(node, list):
        for item in node:
            yield from flatten_messages(item)
    elif isinstance(node, dict):
        for value in node.values():
            if isinstance(value, (list, dict)):
                yield from flatten_messages(value)


def text_parts(part: dict[str, Any]) -> Iterable[str]:
    ctype = str(part.get("content-type") or "")
    content = part.get("content")
    if ctype.startswith("text/plain") and isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for child in content:
            if isinstance(child, dict):
                yield from text_parts(child)


def message_text(msg: dict[str, Any]) -> str:
    pieces: list[str] = []
    for body in msg.get("body") or []:
        if isinstance(body, dict):
            pieces.extend(text_parts(body))
    return "\n\n".join(p.strip() for p in pieces if p.strip()).strip()


def extract_gemini_notes(body: str) -> str:
    lines = body.splitlines()
    start_idx = 0
    for idx, line in enumerate(lines):
        low = line.lower()
        if "auto-generated" in low and "may contain" in low:
            start_idx = idx + 1
            break
        if line.strip() == "Summary":
            start_idx = idx
            break

    end_idx = len(lines)
    for idx in range(start_idx, len(lines)):
        stripped = lines[idx].strip()
        if any(stripped.startswith(marker) for marker in FOOTER_MARKERS):
            end_idx = idx
            break
    return "\n".join(lines[start_idx:end_idx]).strip()


def parse_subject(subject: str) -> tuple[str, dt.date | None]:
    # Notes: "Weekly Pipeline Review" May 18, 2026
    m = re.search(r"Notes:\s*[\"“](.*?)[\"”]\s+([A-Z][a-z]+ \d{1,2}, \d{4})", subject)
    if not m:
        return subject.replace("Notes:", "").strip(), None
    title = m.group(1).strip()
    try:
        day = dt.datetime.strptime(m.group(2), "%B %d, %Y").date()
    except ValueError:
        day = None
    return title, day


def parse_message_datetime(headers: dict[str, str]) -> dt.datetime:
    raw = headers.get("Date", "")
    parsed = email.utils.parsedate_to_datetime(raw) if raw else None
    if parsed is None:
        return dt.datetime.now().astimezone()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    return parsed.astimezone()


def stem_for(msg_dt: dt.datetime, message_id: str) -> str:
    base = msg_dt.strftime("rec-%Y%m%d-%H%M%S")
    suffix = re.sub(r"[^a-zA-Z0-9]+", "", message_id)[:8]
    return f"{base}-{suffix}" if suffix else base


def run_notmuch(query: str, limit: int) -> list[dict[str, Any]]:
    cmd = [
        "notmuch",
        "show",
        "--format=json",
        "--entire-thread=false",
        f"--limit={limit}",
        query,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        print(proc.stderr.strip() or "notmuch show failed", file=sys.stderr)
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(f"notmuch returned invalid JSON: {exc}", file=sys.stderr)
        return []
    return list(flatten_messages(data))


import recorder_config as rc


def notmuch_query(args: Any) -> str:
    if args.query:
        return args.query
    return f"from:{GEMINI_SENDER} AND subject:Notes AND date:{args.days}d.."


def notes_from_notmuch(query: str, limit: int) -> list[dict[str, str]]:
    """Normalize notmuch's nested show-JSON into flat note records."""
    out: list[dict[str, str]] = []
    for msg in run_notmuch(query, limit):
        headers = msg.get("headers") or {}
        out.append(
            {
                "subject": str(headers.get("Subject") or ""),
                "sender": str(headers.get("From") or ""),
                "date": str(headers.get("Date") or ""),
                "message_id": str(
                    msg.get("id") or headers.get("Message-Id") or headers.get("Subject") or ""
                ),
                "body": message_text(msg),
            }
        )
    return out


def notes_from_gmail(days: int, limit: int, account: str) -> list[dict[str, str]]:
    """Same records, read live from the Gmail API.

    Reading Gmail directly means imports don't silently depend on a local maildir sync being current. The
    RFC822 Message-Id is preferred as the identity so a note imported via either
    backend lands on the same stem and can't be duplicated.

    Built from structured params rather than a notmuch query string: the
    translation layer rewrites `AND` but not notmuch's `date:90d..`, which Gmail
    would treat as a literal term and silently return nothing for.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent / "_prep"))
    from email_search import make_email_search

    router = make_email_search(default_account=account)
    results = router.search_emails(
        query="*",
        account=account,
        limit=limit,
        sender=GEMINI_SENDER,
        subject="Notes",
        date_filter=f"{days}d",
        include_body=True,
    )
    out: list[dict[str, str]] = []
    for msg in results:
        out.append(
            {
                "subject": str(msg.get("subject") or ""),
                "sender": str(msg.get("sender") or ""),
                "date": str(msg.get("date") or ""),
                "message_id": str(
                    msg.get("rfc822_message_id") or msg.get("message_id") or ""
                ),
                "body": str(msg.get("body_text") or ""),
            }
        )
    return out


def recording_index(recordings_dir: Path) -> dict[str, str]:
    """Map calendar `event_id` -> recording stem for everything already on disk.

    Shared with `import_meet_notes.py` so the Drive path and the email path can
    both run without importing the same meeting twice: whichever gets there
    first claims the event, and the other skips it.
    """
    index: dict[str, str] = {}
    for path in recordings_dir.glob("*.event.json"):
        try:
            data = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            continue
        event_id = str(data.get("event_id") or "")
        if event_id:
            index[event_id] = path.name.replace(".event.json", "")
    return index


def is_calendar_bound(event_path: Path) -> bool:
    """True when this note's event.json already names a real calendar event."""
    if not event_path.exists():
        return False
    try:
        data = json.loads(event_path.read_text())
    except Exception:  # noqa: BLE001
        return False
    return bool(data.get("calendar_id")) and bool(data.get("attendees"))


def write_import(
    msg: dict[str, str],
    recordings_dir: Path,
    overwrite: bool,
    resolve_event: Any = None,
) -> Path | None:
    subject = msg.get("subject") or ""
    sender = msg.get("sender") or ""
    if GEMINI_SENDER not in sender.lower():
        return None

    notes = extract_gemini_notes(msg.get("body") or "")
    if not notes:
        return None

    msg_dt = parse_message_datetime({"Date": msg.get("date") or ""})
    title, subject_day = parse_subject(subject)
    message_id = msg.get("message_id") or subject
    # The stem stays keyed to the *email*, not the calendar event, so enriching
    # an already-imported note can never rename it into a duplicate.
    stem = stem_for(msg_dt, message_id)
    transcript_path = recordings_dir / f"{stem}.transcript.json"
    event_path = recordings_dir / f"{stem}.event.json"

    already_imported = transcript_path.exists() and not overwrite
    if already_imported and is_calendar_bound(event_path):
        return transcript_path

    calendar_event = None
    if resolve_event is not None:
        day = subject_day or msg_dt.date()
        calendar_event = resolve_event(title or subject, day)

    # Already on disk and we learned nothing new — leave it alone.
    if already_imported and calendar_event is None:
        return transcript_path

    recordings_dir.mkdir(parents=True, exist_ok=True)
    transcript = {
        "text": notes,
        "segments": [
            {
                "start": 0.0,
                "end": 0.0,
                "text": notes,
            }
        ],
    }
    event_start = msg_dt
    if subject_day is not None:
        event_start = event_start.replace(
            year=subject_day.year,
            month=subject_day.month,
            day=subject_day.day,
        )
    event = {
        "title": title or subject,
        "start": event_start.isoformat(),
        "end": (event_start + dt.timedelta(minutes=60)).isoformat(),
        "attendees": [],
        "description": f"Imported from Gemini Notes email: {subject}",
        "meet_url": None,
        "calendar_id": None,
        "event_id": message_id,
    }
    # Bind the note to the calendar event it describes. Without this the note is
    # an island: its event_id is the *email's* Message-Id and its attendee list
    # is empty, so `_recording_match_tier` can only ever reach tier 1 (title) —
    # and external meetings require tier 2, meaning a note could never attach to
    # one. With a real event id and attendees it matches at tier 3.
    if calendar_event is not None:
        event.update(
            {
                "title": calendar_event.get("title") or event["title"],
                "start": calendar_event.get("start") or event["start"],
                "end": calendar_event.get("end") or event["end"],
                "attendees": calendar_event.get("attendees") or [],
                "meet_url": calendar_event.get("meet_url"),
                "calendar_id": calendar_event.get("calendar_id"),
                "event_id": calendar_event.get("event_id") or message_id,
                "gemini_message_id": message_id,
                "match_confidence": calendar_event.get("match_confidence"),
            }
        )
    # On the enrich-in-place path the transcript is unchanged; leave its mtime
    # alone so it doesn't look like a fresh import.
    if not already_imported:
        transcript_path.write_text(
            json.dumps(transcript, ensure_ascii=False, indent=2) + "\n"
        )
    event_path.write_text(json.dumps(event, ensure_ascii=False, indent=2) + "\n")
    return transcript_path


def extract_todos_for(transcript_path: Path) -> tuple[bool, str]:
    """Run extract_todos.py over one imported note, writing `<stem>.todos.json`.

    Imported Gemini notes carry real action items and decisions, but until they
    are normalized into a todos file they're invisible to carryover
    reconciliation and thinner for cross-meeting Q&A. Doing it at import time is
    what keeps the archive uniform without a periodic backfill.

    Soft-fails: an import that succeeded should not be reported as a failure
    because the follow-on model call didn't land.
    """
    stem = transcript_path.name.replace(".transcript.json", "")
    todos_path = transcript_path.with_name(f"{stem}.todos.json")
    if todos_path.exists():
        return False, "already extracted"

    event_path = transcript_path.with_name(f"{stem}.event.json")
    cmd = [
        sys.executable,
        str(Path(__file__).resolve().parent / "extract_todos.py"),
        str(transcript_path),
    ]
    if event_path.exists():
        cmd += ["--event", str(event_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"{type(exc).__name__}"
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        return False, tail[-1] if tail else f"exit {proc.returncode}"
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return False, "non-JSON output"
    todos_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return True, f"{len(data.get('todos') or [])} todos, {len(data.get('decisions') or [])} decisions"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90, help="Lookback window in days")
    parser.add_argument(
        "--query",
        default=None,
        help="notmuch query override (notmuch backend only; defaults to Gemini "
        "Notes within --days)",
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument(
        "--recordings-dir",
        type=Path,
        default=Path(os.environ.get("RECORDER_RECORDINGS_DIR", DEFAULT_RECORDINGS_DIR)),
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--extract",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Extract todos and decisions for imported notes (default: on)",
    )
    parser.add_argument(
        "--extract-workers", type=int, default=4, help="Parallel extraction calls"
    )
    parser.add_argument(
        "--account",
        default=rc.DEFAULT_ACCOUNT,
        help="Mailbox account, used to pick the email backend",
    )
    parser.add_argument(
        "--link-calendar",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Bind each note to its calendar event so it attaches to that "
        "meeting's prep (default: on). Also enriches already-imported notes.",
    )
    parser.add_argument(
        "--backend",
        choices=("auto", "gmail", "notmuch"),
        default="auto",
        help="auto (default) honors EMAIL_SEARCH_BACKEND[_<ACCOUNT>] from "
        "the environment or <data_root>/.env",
    )
    args = parser.parse_args()

    backend = args.backend
    if backend == "auto":
        sys.path.insert(0, str(Path(__file__).resolve().parent / "_prep"))
        try:
            from secret_env import load_project_env

            load_project_env(rc.ENV_FILE)
        except Exception:  # noqa: BLE001 - env file is best-effort
            pass
        from email_search import _backend_for_account

        backend = _backend_for_account(args.account)

    if backend == "gmail":
        try:
            messages = notes_from_gmail(args.days, args.limit, args.account)
        except Exception as exc:  # noqa: BLE001 - never lose the import to auth/network
            print(f"gmail import failed ({exc}); falling back to notmuch", file=sys.stderr)
            backend = "notmuch"
            messages = notes_from_notmuch(notmuch_query(args), args.limit)
    else:
        messages = notes_from_notmuch(notmuch_query(args), args.limit)

    # Resolver is built once and shared: one auth, one client, cached per day.
    resolve_event = None
    if args.link_calendar:
        try:
            import fetch_meeting

            svc = fetch_meeting.calendar_service()
            if svc is not None:
                calendars = fetch_meeting.resolve_calendars(None)
                _day_cache: dict[tuple[str, dt.date], Any] = {}

                def resolve_event(note_title: str, day: dt.date):  # noqa: F811
                    key = (note_title, day)
                    if key not in _day_cache:
                        ev = fetch_meeting.event_by_title_on_day(
                            svc, calendars, note_title, day
                        )
                        _day_cache[key] = fetch_meeting.normalize(ev) if ev else None
                    return _day_cache[key]
        except Exception as exc:  # noqa: BLE001 - linking is enrichment, not the job
            print(f"calendar linking unavailable: {exc}", file=sys.stderr)

    imported: list[str] = []
    linked = 0
    for msg in messages:
        path = write_import(
            msg, args.recordings_dir.expanduser(), args.overwrite, resolve_event
        )
        if path is not None:
            imported.append(str(path))
            if is_calendar_bound(
                path.with_name(path.name.replace(".transcript.json", ".event.json"))
            ):
                linked += 1

    # Covers both fresh imports and any historical note that predates this step,
    # so the archive self-heals instead of needing a separate backfill pass.
    extracted = 0
    failures: list[str] = []
    if args.extract and imported:
        paths = [Path(p) for p in imported]
        workers = max(1, min(args.extract_workers, len(paths)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for path, (ok, detail) in zip(paths, pool.map(extract_todos_for, paths)):
                if ok:
                    extracted += 1
                elif detail != "already extracted":
                    failures.append(f"{path.name}: {detail}")
                    print(f"extract failed for {path.name}: {detail}", file=sys.stderr)

    json.dump(
        {
            "imported": len(imported),
            "files": imported,
            "linkedToCalendar": linked,
            "extracted": extracted,
            "extractFailures": failures,
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
