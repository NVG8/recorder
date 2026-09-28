"""Import "Notes by Gemini" straight from the calendar event's Drive attachment.

Google Meet attaches the Gemini notes doc to the calendar event itself, so the
notes are reachable the moment the meeting ends. That makes this strictly better
than scraping the notification email (`import_gemini_notes.py`), which arrives
minutes-to-hours later — sometimes not the same day — and carries no calendar
identity, forcing a title+date guess to reattach it to its meeting.

Here the meeting *is* the source: the event id, attendees, and times come from
the event the doc hangs off, so the note lands on the right meeting by
construction and matches prep at tier 3.

The email path stays as a fallback for meetings whose doc was never attached, or
where Drive access is unavailable. Both write the same shape, and each skips a
meeting the other already imported.

Usage:
  uv run python import_meet_notes.py --days 7
  uv run python import_meet_notes.py --days 1 --no-extract
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

import fetch_meeting
import google_auth
from import_gemini_notes import extract_todos_for, recording_index

DEFAULT_RECORDINGS_DIR = (
    Path.home() / "Library" / "Application Support" / "Recorder" / "recordings"
)

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
# Titles Google gives the artifacts it attaches to a Meet event.
NOTES_TITLES = ("notes by gemini", "meeting notes", "gemini notes")


def drive_service():
    """Drive client from the shared token registry, or None with a clear reason.

    Returns None rather than raising: a dead Drive token should degrade this
    importer to a no-op and leave the email path working, not break imports.
    `google_auth` prints the specific token and the command to fix it.
    """
    return google_auth.service_for("drive", "drive", "v3")


def notes_attachment(event: dict[str, Any]) -> dict[str, Any] | None:
    """The Gemini notes doc attached to this event, if any."""
    for att in event.get("attachments") or []:
        if att.get("mimeType") != GOOGLE_DOC_MIME:
            continue
        title = (att.get("title") or "").strip().lower()
        if any(t in title for t in NOTES_TITLES):
            return att
    return None


def file_id_from(attachment: dict[str, Any]) -> str:
    if attachment.get("fileId"):
        return str(attachment["fileId"])
    # Older events carry only a fileUrl: .../document/d/<id>/edit?...
    url = str(attachment.get("fileUrl") or "")
    parts = url.split("/d/")
    return parts[1].split("/")[0] if len(parts) > 1 else ""


def fetch_doc_text(svc, file_id: str) -> str:
    data = svc.files().export(fileId=file_id, mimeType="text/plain").execute()
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def doc_modified_at(svc, file_id: str) -> dt.datetime | None:
    try:
        meta = svc.files().get(fileId=file_id, fields="modifiedTime").execute()
        raw = str(meta.get("modifiedTime") or "")
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
    except Exception as exc:  # noqa: BLE001
        print(f"could not read modifiedTime for {file_id}: {exc}", file=sys.stderr)
        return None


def reused_file_ids(events: list[dict[str, Any]]) -> set[str]:
    """File ids attached to more than one event.

    A recurring series can carry a single doc on every instance — one meeting
    here has the same November doc on all 12 instances from March to August.
    Importing that would file stale notes under today's meeting, so when a doc
    can't be tied to one event we decline it and leave the email path (which is
    genuinely per-instance) to cover the meeting.
    """
    counts: dict[str, int] = {}
    for ev in events:
        attachment = notes_attachment(ev)
        file_id = file_id_from(attachment or {})
        if file_id:
            counts[file_id] = counts.get(file_id, 0) + 1
    return {fid for fid, n in counts.items() if n > 1}


def doc_is_fresh_for(
    modified: dt.datetime | None, event_start: dt.datetime, max_age_days: int
) -> bool:
    """Was this doc written around the time of the meeting?

    Catches the single-event version of the staleness problem: a doc attached
    once and never updated. Notes are written during or just after the meeting,
    so a doc last touched well before it is not that meeting's notes.
    """
    if modified is None:
        return True  # Can't tell — don't block on a metadata read failing.
    delta = (modified - event_start).total_seconds() / 86400.0
    return -max_age_days <= delta <= max_age_days


def events_with_notes(svc_cal, calendar_ids: list[str], days: int) -> list[dict[str, Any]]:
    tz = fetch_meeting._local_tz()
    now = dt.datetime.now(tz)
    start = now - dt.timedelta(days=days)
    out: list[dict[str, Any]] = []
    for cal_id in calendar_ids:
        try:
            resp = svc_cal.events().list(
                calendarId=cal_id,
                timeMin=start.isoformat(),
                timeMax=now.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
            ).execute()
        except Exception as exc:  # noqa: BLE001
            print(f"calendar {cal_id} list failed: {exc}", file=sys.stderr)
            continue
        for ev in resp.get("items", []) or []:
            if notes_attachment(ev) is None:
                continue
            ev["_calendar_id"] = cal_id
            out.append(ev)
    return out


def stem_for_event(event_start: dt.datetime) -> str:
    return f"rec-{event_start.strftime('%Y%m%d-%H%M%S')}-meetnotes"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="Lookback window")
    parser.add_argument(
        "--recordings-dir",
        type=Path,
        default=Path(os.environ.get("RECORDER_RECORDINGS_DIR", DEFAULT_RECORDINGS_DIR)),
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--extract", action=argparse.BooleanOptionalAction, default=True
    )
    parser.add_argument(
        "--max-doc-age-days",
        type=int,
        default=2,
        help="Reject a notes doc last modified more than this many days from "
        "the meeting — it belongs to a different occurrence (default: 2)",
    )
    args = parser.parse_args()

    recordings_dir = args.recordings_dir.expanduser()

    svc_cal = fetch_meeting.calendar_service()
    if svc_cal is None:
        json.dump({"imported": 0, "error": "calendar auth unavailable"}, sys.stdout)
        sys.stdout.write("\n")
        return 0

    svc_drive = drive_service()
    if svc_drive is None:
        json.dump(
            {
                "imported": 0,
                "error": "drive auth unavailable — re-authorize the Drive token",
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
        return 0

    calendars = fetch_meeting.resolve_calendars(None)
    events = events_with_notes(svc_cal, calendars, args.days)

    # Whatever the email importer already brought in for these events, so the two
    # paths can run side by side without producing duplicate recordings.
    existing_by_event = recording_index(recordings_dir)

    shared = reused_file_ids(events)
    if shared:
        print(
            f"{len(shared)} doc(s) attached to multiple events; skipping those "
            "(series-level attachment, not per-meeting notes)",
            file=sys.stderr,
        )

    imported: list[str] = []
    skipped = 0
    stale = 0
    for ev in events:
        event_id = str(ev.get("id") or "")
        if not args.overwrite and event_id in existing_by_event:
            skipped += 1
            continue

        attachment = notes_attachment(ev)
        file_id = file_id_from(attachment or {})
        if not file_id or file_id in shared:
            if file_id:
                stale += 1
            continue

        normalized = fetch_meeting.normalize(ev)
        start_raw = normalized.get("start") or ""
        try:
            event_start = dt.datetime.fromisoformat(start_raw)
        except ValueError:
            continue

        if not doc_is_fresh_for(
            doc_modified_at(svc_drive, file_id), event_start, args.max_doc_age_days
        ):
            print(
                f"skipping stale doc for {ev.get('summary')!r} on {start_raw[:10]}",
                file=sys.stderr,
            )
            stale += 1
            continue

        try:
            text = fetch_doc_text(svc_drive, file_id)
        except Exception as exc:  # noqa: BLE001
            print(f"doc fetch failed for {ev.get('summary')}: {exc}", file=sys.stderr)
            continue
        if not text.strip():
            continue

        stem = stem_for_event(event_start.astimezone())
        transcript_path = recordings_dir / f"{stem}.transcript.json"
        event_path = recordings_dir / f"{stem}.event.json"
        if transcript_path.exists() and not args.overwrite:
            skipped += 1
            continue

        recordings_dir.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text(
            json.dumps(
                {
                    "text": text.strip(),
                    "segments": [{"start": 0.0, "end": 0.0, "text": text.strip()}],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        normalized["description"] = (
            normalized.get("description")
            or f"Gemini notes attached to calendar event: {normalized.get('title')}"
        )
        normalized["source"] = "meet_notes_drive"
        normalized["drive_file_id"] = file_id
        event_path.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
        )
        imported.append(str(transcript_path))

    extracted = 0
    failures: list[str] = []
    if args.extract and imported:
        for path in imported:
            ok, detail = extract_todos_for(Path(path))
            if ok:
                extracted += 1
            elif detail != "already extracted":
                failures.append(f"{Path(path).name}: {detail}")

    json.dump(
        {
            "eventsWithNotes": len(events),
            "imported": len(imported),
            "files": imported,
            "skippedExisting": skipped,
            "skippedStale": stale,
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
