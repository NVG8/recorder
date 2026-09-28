"""Find the Google Calendar event that matches a recording timestamp.

One OAuth token can usually read several calendars, so we search every
business calendar listed in `match_calendars` (see recorder_config.py) and pick
the best-overlapping event. A meeting on any of your work calendars matches,
while personal/family calendars are left out to avoid false matches.

Usage:
  uv run fetch_meeting.py <recording-stem>            # e.g. rec-20260518-090127
  uv run fetch_meeting.py --start <iso> --end <iso>   # explicit window
  uv run fetch_meeting.py <stem> --calendar <id>      # restrict to one calendar

Env:
  RECORDER_MATCH_TOKEN_PATH  default: <data_root>/config/calendar_token.json
  RECORDER_CALENDAR_IDS      optional comma-separated list to override the
                             configured business-calendar set.

Output (stdout): JSON. Empty object `{}` if no matching event was found.
  {
    "title": "...",
    "start": "2026-05-18T09:00:00-07:00",
    "end": "2026-05-18T09:30:00-07:00",
    "attendees": [{"email": "...", "name": "..."}, ...],
    "description": "...",
    "meet_url": "...",
    "calendar_id": "...",
    "event_id": "..."
  }
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


import recorder_config as rc

DEFAULT_TOKEN = str(rc.MATCH_CALENDAR_TOKEN)

# The business calendars the token can read. We search these, and only these,
# so personal/family calendars (which hold long overlapping blocks that cause
# false matches) are left out.
DEFAULT_BUSINESS_CALENDARS = list(rc.MATCH_CALENDARS)


def _local_tz() -> dt.tzinfo:
    """Best-effort local timezone (the recording stems are local-time)."""
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def parse_stem(stem: str) -> dt.datetime:
    """Parse `rec-YYYYMMDD-HHMMSS` into a local-time aware datetime."""
    m = re.match(r"(?:rec-)?(\d{8})-(\d{6})", stem)
    if not m:
        raise ValueError(f"unrecognized stem: {stem!r}")
    return dt.datetime.strptime(f"{m.group(1)}-{m.group(2)}", "%Y%m%d-%H%M%S").replace(
        tzinfo=_local_tz()
    )


def overlap_seconds(a_start: dt.datetime, a_end: dt.datetime,
                    b_start: dt.datetime, b_end: dt.datetime) -> float:
    s = max(a_start, b_start)
    e = min(a_end, b_end)
    return max(0.0, (e - s).total_seconds())


def _event_dt(side: dict) -> dt.datetime | None:
    if "dateTime" in side:
        return dt.datetime.fromisoformat(side["dateTime"])
    if "date" in side:
        # All-day event — treat as midnight local; we won't typically match these.
        return dt.datetime.fromisoformat(side["date"]).replace(tzinfo=_local_tz())
    return None


_BUSY_TITLES = ("", "busy", "private", "busy (private)", "(private)")


def _is_real_meeting(ev: dict) -> bool:
    """A call you'd record — has at least one other attendee and isn't a mirrored
    private-busy placeholder. Everything else (personal focus/family blocks with
    no attendees, mirrored "Busy" copies) is demoted so it only wins when there's
    no real meeting to match. This is a REJECT filter, not a tie-breaker between
    two real meetings — start-time proximity decides those."""
    others = [a for a in (ev.get("attendees") or []) if not a.get("self")]
    if not others:
        return False
    return (ev.get("summary") or "").strip().lower() not in _BUSY_TITLES


# How you RSVP'd is the tie-breaker for a double-booking: you don't record a
# meeting you declined. Lower is better; unknown/tentative sits in the middle so
# an explicitly accepted meeting wins and a declined one loses.
_RSVP_RANK = {"accepted": 0, "tentative": 1, "needsaction": 1, "": 1, "declined": 2}


def _rsvp_rank(ev: dict) -> int:
    for a in ev.get("attendees") or []:
        if a.get("self"):
            return _RSVP_RANK.get((a.get("responseStatus") or "").lower(), 1)
    return 1  # no self attendee (e.g. your own calendar) → neutral


def resolve_calendars(explicit: str | None) -> list[str]:
    """Which calendars to search: an explicit --calendar wins; else a pinned
    RECORDER_CALENDAR_IDS env list; else the configured business calendars."""
    if explicit:
        return [explicit]
    env = os.environ.get("RECORDER_CALENDAR_IDS")
    if env:
        return [c.strip() for c in env.split(",") if c.strip()]
    return list(DEFAULT_BUSINESS_CALENDARS)


def best_event(svc, calendar_ids: list[str], win_start: dt.datetime, win_end: dt.datetime) -> dict | None:
    """Pick the event most likely to be the one being recorded, across all the
    given calendars.

    Recording windows are approximate (we don't always know the precise end), so
    a plain "max overlap" can pick the wrong meeting — a long personal block can
    out-overlap the real call, and two real meetings can overlap (double-booked).
    Ranking per candidate:
      1. is it a real meeting (has attendees, not a busy placeholder) — rejects
         personal blocks and mirrored busy copies,
      2. did you accept it (a declined meeting loses a double-booking),
      3. was it already in progress when the recording started (beats a meeting
         that starts later, even one starting soon after),
      4. whose start is closest to when the recording began,
      5. larger overlap as a final tie-break.
    """
    pad = dt.timedelta(hours=2)
    scored: list[tuple[tuple, dict, float, str]] = []
    for calendar_id in calendar_ids:
        try:
            resp = svc.events().list(
                calendarId=calendar_id,
                timeMin=(win_start - pad).isoformat(),
                timeMax=(win_end + pad).isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute()
        except Exception as e:
            print(f"skip calendar {calendar_id}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        for ev in resp.get("items", []):
            s = _event_dt(ev.get("start", {}))
            e = _event_dt(ev.get("end", {}))
            if not s or not e:
                continue
            ov = overlap_seconds(win_start, win_end, s, e)
            if ov <= 0:
                continue
            confidence = min(1.0, ov / max((win_end - win_start).total_seconds(), 1.0))
            # Real meetings first; then accepted over declined; then already in
            # progress at record start; then nearest start; then larger overlap.
            key = (
                0 if _is_real_meeting(ev) else 1,
                _rsvp_rank(ev),
                0 if s <= win_start <= e else 1,
                abs((s - win_start).total_seconds()),
                -ov,
            )
            scored.append((key, ev, confidence, calendar_id))

    if not scored:
        return None
    scored.sort(key=lambda x: x[0])
    _key, ev, confidence, calendar_id = scored[0]
    ev = dict(ev)
    ev["_match_confidence"] = confidence
    ev["_calendar_id"] = calendar_id
    return ev


def _norm_title(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def event_by_title_on_day(
    svc, calendar_ids: list[str], title: str, day: dt.date
) -> dict | None:
    """Find the calendar event named `title` on `day`.

    Used for imported Gemini Notes, where the source email gives a meeting title
    and a date but no times — so the overlap scoring `best_event` relies on has
    nothing to work with. Matching on (title, day) is both simpler and more
    precise for that case.

    Prefers an exact normalized title, then a containment match, so
    "Notes: Marketing Planning Meeting" still finds "Marketing Planning Meeting".
    Returns None when nothing matches or the day holds two equally good
    candidates — a wrong calendar id would silently bind a note to the wrong
    meeting, which is worse than leaving it unlinked.
    """
    target = _norm_title(title)
    if not target:
        return None
    tz = _local_tz()
    start = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
    end = start + dt.timedelta(days=1)

    exact: list[dict] = []
    partial: list[dict] = []
    for cal_id in calendar_ids:
        try:
            resp = svc.events().list(
                calendarId=cal_id,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=100,
            ).execute()
        except Exception as exc:  # noqa: BLE001 - one bad calendar shouldn't stop the rest
            print(f"calendar {cal_id} list failed: {exc}", file=sys.stderr)
            continue
        for ev in resp.get("items", []) or []:
            if not _is_real_meeting(ev):
                continue
            summary = _norm_title(ev.get("summary") or "")
            if not summary:
                continue
            ev["_calendar_id"] = cal_id
            if summary == target:
                exact.append(ev)
            elif summary in target or target in summary:
                partial.append(ev)

    for bucket in (exact, partial):
        if len(bucket) == 1:
            bucket[0]["_match_confidence"] = "title_day"
            return bucket[0]
        if len(bucket) > 1:
            print(
                f"{len(bucket)} events match {title!r} on {day} — leaving unlinked",
                file=sys.stderr,
            )
            return None
    return None


def calendar_service(token_path: str | None = None):
    """Build a Calendar client, or None when the token is missing/unusable.

    Goes through the shared token registry so a dead credential reports which
    token it is and how to fix it, instead of failing anonymously. An explicit
    `token_path` still wins, for the `--token` flag.
    """
    if token_path is None:
        import google_auth

        return google_auth.service_for("match_calendar", "calendar", "v3")
    if not Path(token_path).exists():
        print(f"token not found: {token_path}", file=sys.stderr)
        return None
    try:
        with open(token_path) as fh:
            info = json.load(fh)
        creds = Credentials.from_authorized_user_info(info)
        return build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception as exc:  # noqa: BLE001
        print(f"calendar auth failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def normalize(ev: dict) -> dict:
    calendar_id = ev.get("_calendar_id")
    attendees = []
    for a in ev.get("attendees", []) or []:
        attendees.append({
            "email": a.get("email", ""),
            "name": a.get("displayName"),
            "organizer": bool(a.get("organizer")),
            "self": bool(a.get("self")),
        })
    meet_url = None
    for ep in (ev.get("conferenceData", {}) or {}).get("entryPoints", []) or []:
        if ep.get("entryPointType") == "video" and ep.get("uri", "").startswith("http"):
            meet_url = ep["uri"]
            break
    return {
        "title": ev.get("summary"),
        "start": (ev.get("start") or {}).get("dateTime"),
        "end": (ev.get("end") or {}).get("dateTime"),
        "attendees": attendees,
        "description": ev.get("description"),
        "meet_url": meet_url,
        "calendar_id": calendar_id,
        "event_id": ev.get("id"),
        "match_confidence": ev.get("_match_confidence"),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("stem", nargs="?", help="recording stem, e.g. rec-20260518-090127")
    p.add_argument("--start", help="ISO start (overrides stem)")
    p.add_argument("--end", help="ISO end (defaults to start + audio duration, else +60 min)")
    p.add_argument("--audio", type=Path, help="audio file used to derive recording duration")
    p.add_argument("--token", default=DEFAULT_TOKEN)
    p.add_argument("--calendar", default=None,
                   help="Restrict the search to one calendar id (default: all "
                        "owner/writer calendars on the token).")
    args = p.parse_args()

    if args.start:
        win_start = dt.datetime.fromisoformat(args.start)
    elif args.stem:
        win_start = parse_stem(args.stem)
    else:
        p.error("provide a recording stem or --start")

    if args.end:
        win_end = dt.datetime.fromisoformat(args.end)
    elif args.audio and args.audio.exists():
        try:
            import soundfile as sf
            with sf.SoundFile(str(args.audio)) as f:
                duration = len(f) / float(f.samplerate)
            win_end = win_start + dt.timedelta(seconds=duration)
        except Exception as e:
            print(f"could not read audio duration: {e}; falling back to +60 min", file=sys.stderr)
            win_end = win_start + dt.timedelta(minutes=60)
    else:
        win_end = win_start + dt.timedelta(minutes=60)

    if not Path(args.token).exists():
        print(f"token not found: {args.token}", file=sys.stderr)
        json.dump({}, sys.stdout); sys.stdout.write("\n")
        return 0  # Soft-fail: pipeline continues without enrichment.

    try:
        with open(args.token) as fh:
            info = json.load(fh)
        creds = Credentials.from_authorized_user_info(info)
        svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        calendars = resolve_calendars(args.calendar)
        if not calendars:
            print("no calendars to search", file=sys.stderr)
            json.dump({}, sys.stdout); sys.stdout.write("\n")
            return 0
        print(f"searching {len(calendars)} calendar(s): {', '.join(calendars)}", file=sys.stderr)
        ev = best_event(svc, calendars, win_start, win_end)
    except Exception as e:
        print(f"calendar lookup failed: {type(e).__name__}: {e}", file=sys.stderr)
        json.dump({}, sys.stdout); sys.stdout.write("\n")
        return 0

    if not ev:
        print(
            f"no event overlapping {win_start.isoformat()} → {win_end.isoformat()}",
            file=sys.stderr,
        )
        json.dump({}, sys.stdout); sys.stdout.write("\n")
        return 0

    out = normalize(ev)
    print(f"matched: {out['title']} on {out['calendar_id']} ({out['start']} → {out['end']})", file=sys.stderr)
    json.dump(out, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
