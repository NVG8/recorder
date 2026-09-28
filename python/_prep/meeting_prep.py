#!/usr/bin/env python3
"""
Meeting prep helpers for the Recorder sidecar.

Turns calendar events into Meeting objects and gathers context for each one:
email history, Gemini notes, prior Recorder sessions, and transcripts from
earlier instances of a recurring series. `fetch_meeting_prep.py` is the only
caller; it assembles the results into the JSON the prep panel shows.
"""

from __future__ import annotations

from email.utils import parsedate_to_datetime
import json
import logging
import os
import re
import sys
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Quiet noisy libraries before importing them.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
warnings.filterwarnings("ignore", message="file_cache is only supported")
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# Local imports — match the path setup the rest of the project uses.
SRC_DIR = Path(__file__).parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# VENDORED INTO THE RECORDER SIDECAR. notmuch_search/gmail_search live
# alongside this file (in _prep/), so we deliberately do NOT add any external
# source dirs to sys.path. Keeping this self-contained is the whole point: a
# stale file in another project must not be able to break us. The one exception
# is the sidecar root, for recorder_config.
if str(SRC_DIR.parent) not in sys.path:
    sys.path.insert(0, str(SRC_DIR.parent))

import recorder_config as rc

from secret_env import load_project_env  # noqa: F401  (re-exported for fetch_meeting_prep)

try:
    from notmuch_search import NotmuchSearch
except ImportError:
    NotmuchSearch = None  # type: ignore


# ── Configuration ────────────────────────────────────────────────

CONFIG_DIR = rc.CONFIG_DIR
LOGS_DIR = rc.LOGS_DIR
INTERNAL_MEETINGS_FILE = CONFIG_DIR / "internal_meetings.json"
RECORDER_RECORDINGS_DIR = rc.RECORDINGS_DIR

# How far back / how many prior Recorder sessions to attach to an *external*
# meeting brief (series meetings use the per-series allowlist settings instead).
EXTERNAL_RECORDER_LOOKBACK_DAYS = int(
    os.getenv("MEETING_PREP_RECORDER_LOOKBACK_DAYS", "120")
)
EXTERNAL_RECORDER_LIMIT = int(os.getenv("MEETING_PREP_RECORDER_LIMIT", "3"))


CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

ACCOUNT_CALENDAR_IDS: Dict[str, set[str]] = {
    name: set(rc.account_calendar_ids(name)) for name in rc.ACCOUNTS
}

# How "I" appear in attendee lists for each account.
ACCOUNT_MY_EMAIL: Dict[str, str] = {
    name: (rc.account_emails(name) or [rc.USER_EMAIL])[0] for name in rc.ACCOUNTS
}

# Domains we treat as "internal" (not worth prep enrichment).
INTERNAL_DOMAINS = rc.INTERNAL_DOMAINS



# ── Data classes ─────────────────────────────────────────────────

@dataclass
class Attendee:
    email: str
    display_name: str = ""
    company: str = ""

    @property
    def domain(self) -> str:
        if "@" in self.email:
            return self.email.split("@", 1)[1].lower()
        return ""




@dataclass
class Meeting:
    event_id: str
    title: str
    description: str
    location: str
    start: datetime
    end: datetime
    timezone: str
    organizer_email: str
    html_link: str
    attendees: List[Attendee]
    hangout_link: str = ""
    recurring_event_id: str = ""
    series_entry: Optional[Dict[str, Any]] = None

    @property
    def duration_minutes(self) -> int:
        return max(int((self.end - self.start).total_seconds() // 60), 0)

    @property
    def slug(self) -> str:
        base = re.sub(r"[^a-zA-Z0-9]+", "-", self.title.lower()).strip("-")
        return (base or "meeting")[:60]


# ── Logging ──────────────────────────────────────────────────────

def setup_logging() -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    handlers: List[logging.Handler] = [
        logging.FileHandler(LOGS_DIR / "meeting_prep.log"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )
    return logging.getLogger("meeting_prep")


# ── Calendar ─────────────────────────────────────────────────────

def load_calendar_service(token_path: Path) -> Optional[Any]:
    if not token_path.exists():
        return None
    try:
        token_data = json.loads(token_path.read_text())
        scopes = token_data.get("scopes") or CALENDAR_SCOPES
        creds = Credentials.from_authorized_user_file(str(token_path), scopes=scopes)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            rc.write_secret(token_path, creds.to_json())
        return build("calendar", "v3", credentials=creds)
    except Exception as exc:
        logging.getLogger("meeting_prep").warning(
            "Failed to load calendar token at %s: %s", token_path, exc
        )
        return None


def resolve_calendar_id(service: Any, account: str) -> Optional[str]:
    preferred = {x.lower() for x in ACCOUNT_CALENDAR_IDS.get(account.lower(), set())}

    items = service.calendarList().list().execute().get("items", [])
    for cal in items:
        if cal.get("id", "").lower() in preferred:
            return cal["id"]

    # Fallback: keyword match against summary/id.
    for cal in items:
        cal_id = cal.get("id", "").lower()
        cal_summary = cal.get("summary", "").lower()
        if account.lower() in cal_id or account.lower() in cal_summary:
            return cal["id"]
    return None




def parse_event_datetime(payload: Dict[str, Any]) -> Tuple[datetime, str]:
    if "dateTime" in payload:
        dt = datetime.fromisoformat(payload["dateTime"].replace("Z", "+00:00"))
        return dt, payload.get("timeZone", "")
    if "date" in payload:
        dt = datetime.fromisoformat(payload["date"]).replace(tzinfo=timezone.utc)
        return dt, payload.get("timeZone", "")
    raise ValueError(f"event payload has no time: {payload}")


def is_external_attendee(att: Dict[str, Any], my_email: str) -> bool:
    if att.get("self"):
        return False
    if att.get("resource"):
        return False
    if att.get("responseStatus") == "declined":
        return False
    email = (att.get("email") or "").lower()
    if not email or "@" not in email:
        return False
    if email == my_email.lower():
        return False
    domain = email.split("@", 1)[1]
    if domain in INTERNAL_DOMAINS:
        return False
    return True


def _match_internal_series(
    event: Dict[str, Any], allowlist: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    title = event.get("summary", "") or ""
    rec_id = event.get("recurringEventId") or ""
    for entry in allowlist:
        entry_rec = entry.get("recurring_event_id") or ""
        if entry_rec and rec_id == entry_rec:
            return entry
        pattern = entry.get("title_pattern") or ""
        if pattern and re.search(pattern, title):
            return entry
    return None


def build_meetings(
    events: List[Dict[str, Any]],
    my_email: str,
    logger: logging.Logger,
    internal_allowlist: Optional[List[Dict[str, Any]]] = None,
) -> List[Meeting]:
    allowlist = internal_allowlist or []
    meetings: List[Meeting] = []
    internal_count = 0
    for event in events:
        if event.get("status") == "cancelled":
            continue
        if "dateTime" not in event.get("start", {}):
            continue  # skip all-day events

        attendees_raw = event.get("attendees", []) or []
        external = [
            Attendee(
                email=(a.get("email") or "").lower(),
                display_name=a.get("displayName") or "",
            )
            for a in attendees_raw
            if is_external_attendee(a, my_email)
        ]

        series_entry: Optional[Dict[str, Any]] = None
        attendees_for_meeting: List[Attendee] = external
        if not external:
            series_entry = _match_internal_series(event, allowlist)
            if series_entry is None:
                continue
            # For series-mode meetings, keep all non-self attendees as participants.
            attendees_for_meeting = [
                Attendee(
                    email=(a.get("email") or "").lower(),
                    display_name=a.get("displayName") or "",
                )
                for a in attendees_raw
                if not a.get("self") and not a.get("resource")
                and (a.get("email") or "")
            ]
            internal_count += 1

        start, tz = parse_event_datetime(event["start"])
        end, _ = parse_event_datetime(event["end"])

        meetings.append(
            Meeting(
                event_id=event.get("id", ""),
                title=event.get("summary", "(no title)"),
                description=(event.get("description") or "").strip(),
                location=(event.get("location") or "").strip(),
                start=start,
                end=end,
                timezone=tz,
                organizer_email=(event.get("organizer", {}) or {}).get("email", ""),
                html_link=event.get("htmlLink", ""),
                hangout_link=event.get("hangoutLink", ""),
                attendees=attendees_for_meeting,
                recurring_event_id=event.get("recurringEventId") or "",
                series_entry=series_entry,
            )
        )

    external_count = len(meetings) - internal_count
    logger.info(
        "Found %d external-attendee meeting(s) and %d internal-series meeting(s) in window",
        external_count, internal_count,
    )
    return meetings


# ── Email history (notmuch) ──────────────────────────────────────

def gather_email_history(
    notmuch: Optional["NotmuchSearch"],
    attendee: Attendee,
    account: str,
    logger: logging.Logger,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    if notmuch is None:
        return []
    try:
        # Notmuch query for either direction.
        results = notmuch.search_emails(
            query=f"(from:{attendee.email} OR to:{attendee.email})",
            account=account,
            limit=limit,
            date_filter="180d",
            include_body=True,
        )
        # Trim bodies to keep prompt tight.
        for r in results:
            body = (r.get("body_text") or "").strip()
            r["body_excerpt"] = body[:600]
            r.pop("body_text", None)
        return results
    except Exception as exc:
        logger.warning("notmuch search failed for %s: %s", attendee.email, exc)
        return []


# ── Gemini meeting-notes emails ──────────────────────────────────

GEMINI_NOTES_SENDER = "gemini-notes@google.com"
_GEMINI_FOOTER_MARKERS = (
    "Meeting records",
    "Is the Next Steps section",
    "Google LLC,",
    "You have received this email because meeting artifacts",
)


def _extract_gemini_summary(body: str, max_len: int = 1800) -> str:
    """Strip the Gemini email boilerplate and return the substantive notes."""
    if not body:
        return ""
    lines = body.split("\n")

    start_idx = 0
    for i, line in enumerate(lines):
        low = line.lower()
        if "auto-generated" in low and "may contain" in low:
            start_idx = i + 1
            break
        if line.strip() == "Summary":
            start_idx = i
            break

    end_idx = len(lines)
    for i in range(start_idx, len(lines)):
        stripped = lines[i].strip()
        if any(stripped.startswith(m) for m in _GEMINI_FOOTER_MARKERS):
            end_idx = i
            break

    text = "\n".join(lines[start_idx:end_idx]).strip()
    if len(text) > max_len:
        text = text[: max_len].rstrip() + "…"
    return text


def _resolve_full_name(
    notmuch: Optional["NotmuchSearch"], att: Attendee, account: str
) -> str:
    """Return att.display_name if it's at least two words; otherwise try to
    pull a full name out of the From: header of a recent email from the same
    address. Falls back to whatever we already had."""
    name = (att.display_name or "").strip()
    if len(name.split()) >= 2 or notmuch is None or not att.email:
        return name
    try:
        results = notmuch.search_emails(
            query=f"from:{att.email}",
            account=account,
            limit=1,
            date_filter="365d",
        )
    except Exception:
        return name
    if not results:
        return name
    sender = (results[0].get("sender") or "").strip()
    m = re.match(r'^\s*"?([^"<]+?)"?\s*<', sender)
    if m:
        candidate = m.group(1).strip()
        if len(candidate.split()) >= 2:
            return candidate
    return name


def gather_gemini_notes_for_meeting(
    notmuch: Optional["NotmuchSearch"],
    attendees: List[Attendee],
    account: str,
    logger: logging.Logger,
    per_attendee_limit: int = 5,
    overall_limit: int = 8,
) -> List[Dict[str, Any]]:
    """Return deduped past Gemini meeting notes that include any of the given
    attendees, ordered most-recent first."""
    if notmuch is None or not attendees:
        return []

    seen: Dict[str, Dict[str, Any]] = {}
    for att in attendees:
        # Gemini emails are addressed only to the user; attendees appear inside
        # the body (e.g. "[Jane Doe] Action Item: ..."). Search by display name —
        # a single first name is too noisy, so skip attendees without a full
        # multi-word name. Calendar invites often omit displayName, so fall
        # back to recovering the name from a recent From: header.
        name = _resolve_full_name(notmuch, att, account)
        if len(name.split()) < 2:
            continue
        try:
            results = notmuch.search_emails(
                query=f'from:{GEMINI_NOTES_SENDER} AND body:"{name}"',
                account=account,
                limit=per_attendee_limit,
                date_filter="180d",
                include_body=True,
            )
        except Exception as exc:
            logger.warning("Gemini-notes search failed for %s: %s", name, exc)
            continue

        for r in results:
            mid = r.get("message_id") or f"{r.get('subject')}::{r.get('date')}"
            if mid in seen:
                if name not in seen[mid]["matched_attendees"]:
                    seen[mid]["matched_attendees"].append(name)
                continue
            seen[mid] = {
                "subject": r.get("subject", "").strip(),
                "date": r.get("date", "").strip(),
                "summary": _extract_gemini_summary(r.get("body_text", "")),
                "matched_attendees": [name],
            }

    notes = list(seen.values())
    # Sort by date string (RFC2822) — best-effort, falls back to subject order.
    try:
        from email.utils import parsedate_to_datetime

        def _key(n: Dict[str, Any]) -> Any:
            try:
                return parsedate_to_datetime(n["date"])
            except Exception:
                return datetime.min.replace(tzinfo=timezone.utc)

        notes.sort(key=_key, reverse=True)
    except Exception:
        pass
    return notes[:overall_limit]


# ── Drive transcripts (file scan) ────────────────────────────────

def gather_transcripts_for_attendee(
    transcript_dir: Path, attendee: Attendee, limit: int = 3
) -> List[Dict[str, str]]:
    if not transcript_dir.exists():
        return []

    needles = []
    if attendee.display_name:
        needles.append(attendee.display_name.lower())
        # First name alone is a noisy match; include only if at least 4 chars.
        first = attendee.display_name.split()[0] if attendee.display_name else ""
        if len(first) >= 4:
            needles.append(first.lower())
    if attendee.email:
        needles.append(attendee.email.lower())
        local = attendee.email.split("@", 1)[0]
        if len(local) >= 4:
            needles.append(local.lower())
    if not needles:
        return []

    matches: List[Tuple[Path, int]] = []
    for path in transcript_dir.glob("*.txt"):
        try:
            text = path.read_text(errors="ignore").lower()
        except Exception:
            continue
        score = sum(text.count(n) for n in needles)
        if score:
            matches.append((path, score))

    matches.sort(key=lambda x: x[1], reverse=True)

    out: List[Dict[str, str]] = []
    for path, _score in matches[:limit]:
        try:
            text = path.read_text(errors="ignore")
        except Exception:
            continue
        out.append(
            {
                "file": path.name,
                "excerpt": _excerpt_around_needle(text, needles, max_len=800),
            }
        )
    return out


def _excerpt_around_needle(text: str, needles: List[str], max_len: int = 800) -> str:
    lower = text.lower()
    for needle in needles:
        idx = lower.find(needle)
        if idx >= 0:
            start = max(0, idx - max_len // 2)
            end = min(len(text), start + max_len)
            return text[start:end].strip()
    return text[:max_len].strip()


# ── Series-level transcript matching ─────────────────────────────

_TZ_ABBREV: Dict[str, timezone] = {
    "PST": timezone(timedelta(hours=-8)),
    "PDT": timezone(timedelta(hours=-7)),
    "MST": timezone(timedelta(hours=-7)),
    "MDT": timezone(timedelta(hours=-6)),
    "CST": timezone(timedelta(hours=-6)),
    "CDT": timezone(timedelta(hours=-5)),
    "EST": timezone(timedelta(hours=-5)),
    "EDT": timezone(timedelta(hours=-4)),
    "UTC": timezone.utc,
    "GMT": timezone.utc,
}

_TRANSCRIPT_FILENAME_RE = re.compile(
    r"Meeting started (\d{4})_(\d{2})_(\d{2}) (\d{2})_(\d{2}) (\w+)"
)


def _parse_transcript_filename(name: str) -> Optional[datetime]:
    m = _TRANSCRIPT_FILENAME_RE.match(name)
    if not m:
        return None
    y, mo, d, h, mi, tz = m.groups()
    tzinfo = _TZ_ABBREV.get(tz.upper())
    if tzinfo is None:
        return None
    return datetime(int(y), int(mo), int(d), int(h), int(mi), tzinfo=tzinfo)


def _list_series_instances(
    service: Any,
    calendar_id: str,
    meeting: Meeting,
    lookback_days: int,
    logger: logging.Logger,
) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=lookback_days)).isoformat()
    time_max = now.isoformat()
    try:
        if meeting.recurring_event_id:
            resp = service.events().instances(
                calendarId=calendar_id,
                eventId=meeting.recurring_event_id,
                timeMin=time_min,
                timeMax=time_max,
                maxResults=50,
            ).execute()
            return resp.get("items", [])
        resp = service.events().list(
            calendarId=calendar_id,
            q=meeting.title,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        ).execute()
        return [
            inst for inst in resp.get("items", [])
            if (inst.get("summary") or "") == meeting.title
        ]
    except Exception as exc:
        logger.warning("Series instance lookup failed for %s: %s", meeting.title, exc)
        return []


def gather_series_transcripts(
    transcript_dir: Path,
    service: Any,
    calendar_id: str,
    meeting: Meeting,
    lookback_days: int,
    limit: int,
    logger: logging.Logger,
) -> List[Dict[str, str]]:
    if not transcript_dir.exists():
        return []

    instances = _list_series_instances(
        service, calendar_id, meeting, lookback_days, logger
    )
    if not instances:
        return []

    indexed: List[Tuple[datetime, Path]] = []
    for path in transcript_dir.glob("*.txt"):
        dt = _parse_transcript_filename(path.name)
        if dt is not None:
            indexed.append((dt, path))

    tolerance = timedelta(minutes=10)
    matched: List[Tuple[datetime, Path]] = []
    used_paths: set = set()
    for inst in instances:
        try:
            inst_start, _ = parse_event_datetime(inst.get("start", {}))
        except ValueError:
            continue
        best: Optional[Tuple[datetime, Path, timedelta]] = None
        for dt, path in indexed:
            if path in used_paths:
                continue
            delta = abs(dt - inst_start)
            if delta <= tolerance and (best is None or delta < best[2]):
                best = (inst_start, path, delta)
        if best is not None:
            matched.append((best[0], best[1]))
            used_paths.add(best[1])

    matched.sort(key=lambda x: x[0], reverse=True)

    out: List[Dict[str, str]] = []
    for start, path in matched[:limit]:
        try:
            text = path.read_text(errors="ignore")
        except Exception:
            continue
        out.append({
            "date": start.astimezone().strftime("%Y-%m-%d"),
            "file": path.name,
            "excerpt": text[:2000].strip(),
        })
    return out


def _read_recorder_transcript(path: Path) -> str:
    try:
        data = json.loads(path.read_text())
    except Exception:
        return ""
    return str(data.get("text") or "").strip()


def _read_recorder_event(transcript_path: Path) -> Dict[str, Any]:
    event_path = transcript_path.with_name(
        transcript_path.name.replace(".transcript.json", ".event.json")
    )
    if not event_path.exists():
        return {}
    try:
        data = json.loads(event_path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def _norm_title(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def _read_recorder_todos(transcript_path: Path) -> Dict[str, Any]:
    todos_path = transcript_path.with_name(
        transcript_path.name.replace(".transcript.json", ".todos.json")
    )
    if not todos_path.exists():
        return {}
    try:
        data = json.loads(todos_path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _recording_match_tier(
    event: Dict[str, Any], meeting: Meeting, match_emails: set
) -> Optional[int]:
    """Score how strongly a Recorder recording's event.json matches `meeting`.

    3 = same calendar event — exact event id, or a prior instance of the same
        recurring series (Google instance ids are `<recurring_id>_<timestamp>`);
    2 = shares at least one relevant attendee email (external counterparties for
        external meetings, participants for series meetings);
    1 = identical normalized title (legacy fallback).
    None = no match.
    """
    rec_event_id = str(event.get("event_id") or "")
    if rec_event_id:
        if meeting.event_id and rec_event_id == meeting.event_id:
            return 3
        if meeting.recurring_event_id and rec_event_id.startswith(
            meeting.recurring_event_id + "_"
        ):
            return 3
    if match_emails:
        rec_emails = {
            (a.get("email") or "").lower()
            for a in (event.get("attendees") or [])
            if a.get("email")
        }
        if rec_emails & match_emails:
            return 2
    if _norm_title(str(event.get("title") or "")) == _norm_title(meeting.title):
        return 1
    return None


def _format_recorder_decisions(
    todos_data: Dict[str, Any], max_items: int = 6
) -> List[str]:
    """Decisions captured from a prior session.

    Distinct from action items on purpose: "we chose vendor A" is the thing a
    later meeting needs to not relitigate, and it reads very differently from
    "someone owes a doc". Absent on recordings extracted before decisions were
    captured, which is indistinguishable from a meeting that decided nothing.
    """
    lines: List[str] = []
    for decision in (todos_data.get("decisions") or [])[:max_items]:
        text = str(decision.get("text") or "").strip()
        if text:
            lines.append(text)
    return lines


def _format_recorder_todos(todos_data: Dict[str, Any], max_items: int = 8) -> List[str]:
    """Render extract_todos output into compact `Owner — task (bucket, due)` lines."""
    lines: List[str] = []
    for todo in (todos_data.get("todos") or [])[:max_items]:
        text = str(todo.get("text") or "").strip()
        if not text:
            continue
        owner = str(todo.get("owner") or "").strip() or "Unassigned"
        bucket = str(todo.get("bucket") or "").strip()
        due = str(todo.get("due_iso") or todo.get("due") or "").strip()
        meta = ", ".join(x for x in (bucket, f"due {due}" if due else "") if x)
        lines.append(f"{owner} — {text}" + (f" ({meta})" if meta else ""))
    return lines


def gather_recorder_context(
    recorder_dir: Path,
    meeting: Meeting,
    lookback_days: int,
    limit: int,
    logger: logging.Logger,
    match_emails: Optional[set] = None,
    min_tier: int = 1,
) -> List[Dict[str, Any]]:
    """Return prior Recorder sessions relevant to `meeting`, newest first.

    Matches recordings to the meeting by (priority order) calendar event id /
    recurring-series id, then shared attendee email, then identical title — see
    `_recording_match_tier`. Each item carries a transcript excerpt (or the
    extract_todos summary, which is cleaner) plus the structured action items
    that were already pulled from that session, so the brief can reuse them
    instead of re-deriving from raw transcript. Serves external and series
    meetings alike; also picks up imported Gemini Notes (transcript/event JSON
    without local audio).

    `min_tier` drops weaker matches: external callers pass 2 so a generic shared
    title alone can't pull in a different company's recording (they always have
    reliable attendee emails); the series path keeps 1 because imported Gemini
    Notes match on title only.
    """
    if not recorder_dir.exists():
        return []

    match_emails = {e.lower() for e in (match_emails or set()) if e}
    current_start = meeting.start.astimezone()
    earliest = current_start - timedelta(days=lookback_days)

    scored: List[Tuple[int, datetime, Path, Dict[str, Any]]] = []
    for path in recorder_dir.glob("rec-*.transcript.json"):
        event = _read_recorder_event(path)
        tier = _recording_match_tier(event, meeting, match_emails)
        if tier is None or tier < min_tier:
            continue
        event_start = _parse_iso_datetime(event.get("start"))
        if event_start is None:
            continue
        event_start = event_start.astimezone(current_start.tzinfo)
        if not (earliest <= event_start < current_start):
            continue
        scored.append((tier, event_start, path, event))

    # Strongest match tier first, then most recent.
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

    out: List[Dict[str, Any]] = []
    for tier, event_start, path, event in scored[:limit]:
        todos_data = _read_recorder_todos(path)
        summary = str(todos_data.get("summary") or "").strip()
        excerpt = (summary or _read_recorder_transcript(path))[:2000].strip()
        if not excerpt:
            continue
        out.append({
            "date": event_start.strftime("%Y-%m-%d"),
            "file": f"Recorder:{path.name}",
            "title": str(event.get("title") or "").strip(),
            "match_tier": tier,
            "excerpt": excerpt,
            "todos": _format_recorder_todos(todos_data),
            "decisions": _format_recorder_decisions(todos_data),
        })
    if out:
        logger.info("Recorder sessions matched: %d for %s", len(out), meeting.title)
    return out


def gather_gemini_notes_for_series(
    notmuch: Optional["NotmuchSearch"],
    meeting: Meeting,
    account: str,
    lookback_days: int,
    limit: int,
    logger: logging.Logger,
) -> List[Dict[str, str]]:
    """Find Gemini Notes emails by recurring meeting title.

    The internal-series path cannot rely on external attendee names, so search
    the Gemini subject directly, e.g. Notes: "Weekly Pipeline Review" May 18.
    """
    if notmuch is None or not meeting.title:
        return []

    try:
        results = notmuch.search_emails(
            query=f'from:{GEMINI_NOTES_SENDER} AND subject:"{meeting.title}"',
            account=account,
            limit=limit * 3,
            date_filter=f"{lookback_days}d",
            include_body=True,
        )
    except Exception as exc:
        logger.warning("Gemini series-notes search failed for %s: %s", meeting.title, exc)
        return []

    current_start = meeting.start.astimezone()
    earliest = current_start - timedelta(days=lookback_days)
    matched: List[Tuple[datetime, Dict[str, Any]]] = []
    for result in results:
        subject = (result.get("subject") or "").strip()
        if meeting.title.lower() not in subject.lower():
            continue
        raw_date = (result.get("date") or "").strip()
        try:
            msg_dt = parsedate_to_datetime(raw_date).astimezone(current_start.tzinfo)
        except Exception:
            msg_dt = datetime.min.replace(tzinfo=current_start.tzinfo)
        if not (earliest <= msg_dt < current_start):
            continue
        matched.append((msg_dt, result))

    matched.sort(key=lambda x: x[0], reverse=True)
    out: List[Dict[str, str]] = []
    for msg_dt, result in matched[:limit]:
        summary = _extract_gemini_summary(result.get("body_text", ""))
        if not summary:
            continue
        out.append({
            "date": msg_dt.strftime("%Y-%m-%d"),
            "file": (result.get("subject") or "Gemini Notes").strip(),
            "excerpt": summary[:2000].strip(),
        })
    if out:
        logger.info("Gemini series notes matched: %d for %s", len(out), meeting.title)
    return out


def dedupe_series_transcripts(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen: set[Tuple[str, str]] = set()
    out: List[Dict[str, str]] = []
    for item in items:
        excerpt = re.sub(r"\s+", " ", (item.get("excerpt") or "")).strip()
        key = (item.get("date") or "", excerpt[:300])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# ── Salesforce ───────────────────────────────────────────────────



# ── Internal-meeting allowlist ───────────────────────────────────

def load_internal_meetings_allowlist(logger: logging.Logger) -> List[Dict[str, Any]]:
    if not INTERNAL_MEETINGS_FILE.exists():
        return []
    try:
        data = json.loads(INTERNAL_MEETINGS_FILE.read_text())
    except Exception as exc:
        logger.warning("Failed to parse %s: %s", INTERNAL_MEETINGS_FILE, exc)
        return []
    if not isinstance(data, list):
        logger.warning("%s is not a JSON list; ignoring", INTERNAL_MEETINGS_FILE)
        return []
    return data


# ── LinkedIn company URL (subprocess to LI venv) ─────────────────







# ── Synthesis ────────────────────────────────────────────────────







# ── Brief assembly ───────────────────────────────────────────────





# ── Email digest via msmtp ───────────────────────────────────────











# ── Orchestration ────────────────────────────────────────────────







