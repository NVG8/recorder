"""Fetch meeting prep data for the Recorder app.

This is intentionally a structured, fast version of the daily meeting prep
pipeline. It reuses _prep/meeting_prep's calendar/notmuch/transcript matching,
but returns JSON for the app instead of sending email.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
CACHE_DIR = Path.home() / "Library" / "Application Support" / "Recorder"

import recorder_config as rc  # noqa: E402

# Optional daily briefs and a meeting-transcript archive written by other tools
# are read from the data root (read-only). The CODE the sidecar depends on is
# vendored under _prep/ so a change in another project can never break prep.
DATA_ROOT = rc.DATA_ROOT
BRIEFS_ROOT = rc.BRIEFS_DIR

# Vendored, self-contained sidecar code (meeting_prep + its helpers). This is the only path we add for imports.
_PREP_DIR = Path(__file__).resolve().parent / "_prep"
if str(_PREP_DIR) not in sys.path:
    sys.path.insert(0, str(_PREP_DIR))

import meeting_prep as prep  # noqa: E402

import prep_synthesis  # noqa: E402


# Key under which each item carries the raw evidence for LLM synthesis. Popped
# before the response is serialized — the app never sees it.
EVIDENCE_KEY = "_evidence"


def meeting_evidence(meeting: prep.Meeting, kind: str) -> dict[str, Any]:
    """Seed the evidence dict with the facts every meeting has."""
    start_local = meeting.start.astimezone()
    return {
        "kind": kind,
        "title": meeting.title,
        "when": start_local.strftime("%A %Y-%m-%d %H:%M %Z").strip(),
        "durationMinutes": meeting.duration_minutes,
        "location": meeting.location,
        "description": meeting.description,
        "attendees": [attendee_payload(a) for a in meeting.attendees],
        "emails": [],
        "notes": [],
        "priorActions": [],
    }


# ---------------------------------------------------------------------------
# Daily brief adapter
#
# If you already generate daily meeting briefs elsewhere, have that tool write
# Markdown to BRIEFS_ROOT/YYYY-MM-DD/HHMM_<slug>.md. Each brief is far richer than the
# recorder's generated prep (deal context, named stakeholders, talking points
# grounded in the specific business). When a brief exists for the meeting we're
# previewing, we lift its section content into the prep dict so the UI shows
# the same quality material the user already sees in email each morning.
# ---------------------------------------------------------------------------


@dataclass
class Brief:
    path: Path
    location: str = ""
    calendar_link: str = ""
    attendees: list[dict[str, str]] = field(default_factory=list)
    why: str = ""
    background: list[str] = field(default_factory=list)
    talking_points: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    suggested_asks: list[str] = field(default_factory=list)

    @property
    def has_narrative(self) -> bool:
        """True when the brief actually carries prose worth showing.

        The brief generator may write a file even when its LLM call fails — the
        body is just `_Brief synthesis failed: ..._`. Those parse to an empty
        Brief, and treating them as real would both display nothing useful and
        suppress the Bedrock synthesis that would have filled the gap.
        """
        return bool(
            self.why
            or self.background
            or self.talking_points
            or self.open_questions
            or self.suggested_asks
        )


def _strip_inline_md(value: str) -> str:
    value = re.sub(r"\*\*(.+?)\*\*", r"\1", value)        # **bold**
    value = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"\1", value)  # *italic*
    value = re.sub(r"`([^`]+)`", r"\1", value)            # `code`
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)  # [text](url)
    return value.strip()


def find_brief_for_meeting(meeting: prep.Meeting) -> Brief | None:
    start_local = meeting.start.astimezone()
    day_dir = BRIEFS_ROOT / start_local.strftime("%Y-%m-%d")
    if not day_dir.exists():
        return None
    prefix = start_local.strftime("%H%M") + "_"
    for path in sorted(day_dir.glob(f"{prefix}*.md")):
        brief = parse_brief(path)
        # A failure-stub brief is worse than no brief: it displays nothing and
        # would suppress synthesis. Fall through so Bedrock writes one instead.
        return brief if brief.has_narrative else None
    return None


def parse_brief(path: Path) -> Brief:
    """Parse a daily brief into section buckets.

    The format is stable (see the README's "Daily briefs"): an H1 title, a few
    metadata bullets, then `## Why this meeting matters`, `## Background`,
    `## Talking points`, `## Open questions`, `## Suggested ask` sections.
    """
    brief = Brief(path=path)
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        if not current:
            location_match = re.match(r"^\s*-\s+\*\*Location\*\*:\s+(.+)$", line)
            if location_match:
                brief.location = _strip_inline_md(location_match.group(1))
                continue
            calendar_match = re.match(r"^\s*-\s+\*\*Calendar\*\*:\s+\[[^\]]+\]\(([^)]+)\)", line)
            if calendar_match:
                brief.calendar_link = calendar_match.group(1).strip()
                continue
        if line.startswith("## "):
            heading = line[3:].strip().lower()
            if "why this meeting matters" in heading or heading == "why this matters":
                current = "why"
            elif heading.startswith("attendees"):
                current = "attendees"
            elif heading.startswith("background"):
                current = "background"
            elif heading.startswith("talking points"):
                current = "talking_points"
            elif heading.startswith("open questions"):
                current = "open_questions"
            elif heading.startswith("suggested ask") or heading.startswith("suggested next"):
                current = "suggested_asks"
            else:
                current = None
            if current is not None:
                sections.setdefault(current, [])
            continue
        if current is not None:
            sections.setdefault(current, []).append(line)

    for line in sections.get("attendees", []):
        match = re.match(r"^\s*[-*]\s+(.+)$", line)
        if not match:
            continue
        parts = [p.strip() for p in match.group(1).split(" — ")]
        if not parts:
            continue
        name = _strip_inline_md(parts[0])
        email = ""
        company = ""
        if len(parts) > 1:
            email = _strip_inline_md(parts[1])
        if len(parts) > 2:
            company = _strip_inline_md(parts[2]).removeprefix("@").strip()
        if name or email:
            brief.attendees.append({"name": name, "email": email, "company": company})

    if "why" in sections:
        joined = " ".join(s.strip() for s in sections["why"] if s.strip())
        brief.why = _strip_inline_md(re.sub(r"\s+", " ", joined))

    for key in ("background", "talking_points", "open_questions", "suggested_asks"):
        for line in sections.get(key, []):
            match = re.match(r"^\s*[-*]\s+(.+)$", line)
            if match:
                cleaned = _strip_inline_md(match.group(1))
                if cleaned:
                    getattr(brief, key).append(cleaned)
    return brief


def apply_brief(item: dict[str, Any], brief: Brief) -> None:
    """Overlay brief content onto a prep item. Brief beats generated prep."""
    if brief.location:
        item["location"] = brief.location
    if brief.calendar_link:
        item["calendarLink"] = brief.calendar_link
    if brief.attendees:
        item["attendees"] = brief.attendees
    if brief.why:
        item["why"] = brief.why
    if brief.background:
        item["leftOff"] = brief.background[:8]
        item["background"] = brief.background[:8]
    if brief.talking_points:
        item["talkingPoints"] = brief.talking_points[:8]
    if brief.open_questions:
        item["openQuestions"] = brief.open_questions[:6]
    if brief.suggested_asks:
        new_actions = [
            {"owner": rc.USER_FIRST_NAME, "text": ask, "source": "Suggested ask"}
            for ask in brief.suggested_asks
        ]
        item["actions"] = new_actions[:6]
        item["suggestedAsk"] = new_actions[:6]
    item["prepState"] = "Brief"
    item["sourceState"] = "Brief"
    item["prepSources"] = prep_sources("Brief", str(brief.path))
    item["briefPath"] = str(brief.path)


# Selectable lookahead windows for the app's "upcoming" view. Each value maps to
# a [start, end) range, always anchored at the start of today (we never show past
# events). Keep these keys in sync with the Swift PrepRange enum.
VALID_RANGES = ("today", "week", "next7")
DEFAULT_RANGE = "today"


def range_window(range_key: str) -> tuple[datetime, datetime]:
    """Return the [start, end) local-time window for a range key."""
    now_local = datetime.now().astimezone()
    start = datetime.combine(now_local.date(), time.min, tzinfo=now_local.tzinfo)
    if range_key == "today":
        end = start + timedelta(days=1)
    elif range_key == "week":
        # Through the end of the current calendar week (Mon–Sun), i.e. up to next Monday.
        end = start + timedelta(days=7 - start.weekday())
    elif range_key == "next7":
        end = start + timedelta(days=7)
    else:
        raise ValueError(f"unknown range {range_key!r}; expected one of {VALID_RANGES}")
    return start, end


def fetch_events_in_range(service: Any, calendar_id: str, range_key: str) -> list[dict[str, Any]]:
    start, end = range_window(range_key)
    resp = service.events().list(
        calendarId=calendar_id,
        timeMin=start.astimezone(timezone.utc).isoformat(),
        timeMax=end.astimezone(timezone.utc).isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=250,
    ).execute()
    return resp.get("items", [])


def day_fields(start_local: datetime) -> dict[str, Any]:
    """Grouping/sorting metadata so the UI can bucket items by day."""
    today = datetime.now().astimezone().date()
    delta = (start_local.date() - today).days
    if delta == 0:
        label = "Today"
    elif delta == 1:
        label = "Tomorrow"
    else:
        label = start_local.strftime("%a, %b %-d")
    return {
        "dayLabel": label,
        "startDate": start_local.strftime("%Y-%m-%d"),
        "startEpoch": start_local.timestamp(),
    }


def event_start_local(event: dict[str, Any]) -> datetime | None:
    try:
        start, _ = prep.parse_event_datetime(event["start"])
        return start.astimezone()
    except Exception:
        return None


def cache_path(account: str, range_key: str = DEFAULT_RANGE) -> Path:
    return CACHE_DIR / f"prep-cache-{account}-{range_key}.json"


def meeting_id(meeting: prep.Meeting) -> str:
    if meeting.event_id:
        return meeting.event_id
    return f"{meeting.start.astimezone().strftime('%Y%m%d-%H%M')}-{meeting.slug}"


def time_range(meeting: prep.Meeting) -> str:
    start = meeting.start.astimezone()
    end = meeting.end.astimezone()
    if start.date() == datetime.now().astimezone().date():
        return f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"
    return f"{start.strftime('%a %H:%M')}-{end.strftime('%H:%M')}"


def event_time_range(event: dict[str, Any]) -> str:
    try:
        start, _ = prep.parse_event_datetime(event["start"])
        end, _ = prep.parse_event_datetime(event["end"])
        return time_range(
            prep.Meeting(
                event_id=event.get("id", ""),
                title=event.get("summary", "(no title)"),
                description="",
                location="",
                start=start,
                end=end,
                timezone="",
                organizer_email="",
                html_link="",
                attendees=[],
            )
        )
    except Exception:
        return ""


def attendee_payload(attendee: prep.Attendee) -> dict[str, str]:
    return {"name": attendee.display_name, "email": attendee.email, "company": attendee.domain}


def prep_sources(state: str, brief_path: str = "") -> list[str]:
    sources = ["Google Calendar", "notmuch", "Gemini Notes", "Recorder transcripts"]
    if state == "Brief":
        sources.insert(0, "daily brief")
    if brief_path:
        sources.append(brief_path)
    return sources


def skipped_item(event: dict[str, Any], reason: str) -> dict[str, Any]:
    event_id = event.get("id") or f"skipped-{clean_line(event.get('summary') or 'meeting', 40)}"
    location = (event.get("location") or "").strip()
    attendees = [
        {"name": a.get("displayName") or "", "email": a.get("email") or "", "company": ""}
        for a in event.get("attendees", []) or []
        if (a.get("email") or "") and not a.get("resource")
    ]
    return {
        "id": event_id,
        "title": event.get("summary") or "(no title)",
        "timeRange": event_time_range(event),
        "location": location,
        "calendarLink": event.get("htmlLink") or "",
        "attendees": attendees,
        "prepState": "Skipped",
        "skipReason": reason,
        "why": reason,
        "leftOff": [],
        "actions": [],
        "talkingPoints": [],
        "openQuestions": [],
        "relatedTranscripts": [],
        "relatedEmails": [],
        "context": [],
        "sourceState": "Skipped",
        "prepSources": ["Google Calendar"],
    }


def event_skip_reason(
    event: dict[str, Any],
    my_email: str,
    internal_allowlist: list[dict[str, Any]],
) -> str | None:
    if event.get("status") == "cancelled":
        return "Cancelled calendar event"
    if "dateTime" not in event.get("start", {}):
        return "All-day event"
    attendees = event.get("attendees", []) or []
    external = [a for a in attendees if prep.is_external_attendee(a, my_email)]
    if external:
        return None
    if prep._match_internal_series(event, internal_allowlist) is not None:
        return None
    if not attendees:
        return "No attendees and not an allowlisted internal series"
    return "No external attendees and not an allowlisted internal series"


def diagnostics_for_events(
    events: list[dict[str, Any]],
    my_email: str,
    internal_allowlist: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in events:
        reason = event_skip_reason(event, my_email, internal_allowlist)
        attendees = event.get("attendees", []) or []
        out.append(
            {
                "id": event.get("id", ""),
                "title": event.get("summary") or "(no title)",
                "timeRange": event_time_range(event),
                "status": event.get("status", ""),
                "eligible": reason is None,
                "skipReason": reason or "",
                "externalAttendees": [
                    a.get("email") or ""
                    for a in attendees
                    if prep.is_external_attendee(a, my_email)
                ],
                "attendees": [a.get("email") or "" for a in attendees],
            }
        )
    return out


# Google Calendar notification mail. These match a meeting's attendees perfectly,
# so they dominate the email history for recurring meetings — but "the organizer
# moved this again" is never prep. Drop them before they reach the brief.
_CALENDAR_NOISE_SUBJECT = re.compile(
    r"^\s*(updated\s+invitation|invitation|accepted|declined|tentatively\s+accepted"
    r"|canceled\s+event|cancelled\s+event|new\s+time\s+proposed|note\s+from)\b",
    re.IGNORECASE,
)
_CALENDAR_NOISE_BODY = re.compile(
    r"this event has been (updated|changed)|has been (updated|changed) with a note",
    re.IGNORECASE,
)


def is_calendar_noise(subject: str, body: str = "") -> bool:
    if _CALENDAR_NOISE_SUBJECT.search(subject or ""):
        return True
    return bool(_CALENDAR_NOISE_BODY.search(body or ""))


def actions_from_recorder_todos(
    todos: list[str], source: str, limit: int = 6
) -> list[dict[str, str]]:
    """Convert `gather_recorder_context` todo lines into action dicts.

    Those lines come from extract_todos' structured output (`Owner — task
    (bucket, due)`), so they're already owner-attributed and deduped — much
    better material than re-parsing `[Owner] task` out of raw transcript text.
    """
    actions: list[dict[str, str]] = []
    for line in todos[:limit]:
        owner, sep, task = line.partition(" — ")
        if not sep:
            owner, task = "", line
        task = clean_line(task, 260)
        if task:
            actions.append(
                {"owner": clean_line(owner, 80) or "Unassigned", "text": task, "source": source}
            )
    return actions


def clean_line(value: str, max_len: int = 180) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    if len(value) > max_len:
        return value[: max_len - 1].rstrip() + "..."
    return value


def summary_sentences(text: str, limit: int = 4) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        line = clean_line(line)
        if not line or line.lower() in {"summary", "suggested next steps"}:
            continue
        if line.startswith("["):
            continue
        if len(line) < 24:
            continue
        out.append(line)
        if len(out) >= limit:
            break
    return out


def action_items_from_text(text: str, source: str, limit: int = 6) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    current_owner = ""
    current_parts: list[str] = []

    def flush() -> None:
        nonlocal current_owner, current_parts
        if not current_owner or not current_parts:
            current_owner = ""
            current_parts = []
            return
        task = clean_line(" ".join(current_parts), 260)
        if task:
            actions.append({"owner": current_owner, "text": task, "source": source})
        current_owner = ""
        current_parts = []

    for raw in text.splitlines():
        line = clean_line(raw, 260)
        match = re.match(r"^\[([^\]]+)\]\s*(.+)$", line)
        if match:
            flush()
            current_owner = clean_line(match.group(1), 80)
            current_parts = [match.group(2)]
        elif current_owner and line and not line.lower().startswith("what do you think"):
            current_parts.append(line)
        else:
            flush()
        if len(actions) >= limit:
            break
    flush()

    cleaned: list[dict[str, str]] = []
    for action in actions:
        if len(cleaned) >= limit:
            break
        if action["text"]:
            cleaned.append(action)
    return cleaned


def external_item(
    meeting: prep.Meeting,
    notmuch: Any,
    account: str,
    logger: Any,
) -> dict[str, Any]:
    related_emails: list[str] = []
    related_transcripts: list[str] = []
    context: list[str] = []
    left_off: list[str] = []
    actions: list[dict[str, str]] = []

    evidence = meeting_evidence(meeting, "external")

    transcript_dir = DATA_ROOT / "meeting_transcripts"
    for attendee in meeting.attendees:
        label = attendee.display_name or attendee.email
        if label:
            context.append(f"Attendee: {label}")
        for email in prep.gather_email_history(notmuch, attendee, account, logger, limit=3):
            subject = clean_line(email.get("subject") or "")
            body = (email.get("body_excerpt") or "").strip()
            if is_calendar_noise(subject, body):
                continue
            if subject and subject not in related_emails:
                related_emails.append(subject)
            if body:
                evidence["emails"].append(
                    {
                        "subject": subject or "(no subject)",
                        "date": email.get("date") or "",
                        "excerpt": body,
                    }
                )
            excerpt = clean_line(body)
            if excerpt and len(left_off) < 4:
                left_off.append(excerpt)
        for tr in prep.gather_transcripts_for_attendee(transcript_dir, attendee, limit=2):
            label = f"{tr.get('file', 'Transcript')}"
            if label not in related_transcripts:
                related_transcripts.append(label)
            body = (tr.get("excerpt") or "").strip()
            if body:
                evidence["notes"].append({"source": label, "date": "", "excerpt": body})

    gemini_notes = prep.gather_gemini_notes_for_meeting(
        notmuch, meeting.attendees, account, logger, overall_limit=4
    )
    for note in gemini_notes:
        subject = clean_line(note.get("subject") or "Gemini Notes")
        related_transcripts.append(subject)
        summary = note.get("summary") or ""
        if summary.strip():
            evidence["notes"].append(
                {"source": subject, "date": note.get("date") or "", "excerpt": summary.strip()}
            )
        for line in summary_sentences(summary, limit=2):
            if len(left_off) < 5:
                left_off.append(line)
        note_actions = action_items_from_text(summary, subject, limit=4)
        evidence["priorActions"].extend(note_actions)
        actions.extend(note_actions)

    # Prior Recorder sessions with these same counterparties. min_tier=2 means a
    # shared generic title alone can't pull in a different company's recording —
    # external meetings always have reliable attendee emails to match on.
    for session in prep.gather_recorder_context(
        prep.RECORDER_RECORDINGS_DIR,
        meeting,
        lookback_days=prep.EXTERNAL_RECORDER_LOOKBACK_DAYS,
        limit=prep.EXTERNAL_RECORDER_LIMIT,
        logger=logger,
        match_emails={a.email for a in meeting.attendees if a.email},
        min_tier=2,
    ):
        source = clean_line(session.get("title") or session.get("file") or "Recorder session")
        if source not in related_transcripts:
            related_transcripts.append(source)
        excerpt = (session.get("excerpt") or "").strip()
        if excerpt or session.get("decisions"):
            evidence["notes"].append(
                {
                    "source": source,
                    "date": session.get("date") or "",
                    "excerpt": excerpt,
                    "decisions": session.get("decisions") or [],
                }
            )
        for line in summary_sentences(excerpt, limit=2):
            if len(left_off) < 6:
                left_off.append(line)
        session_actions = actions_from_recorder_todos(
            session.get("todos") or [], source, limit=4
        )
        evidence["priorActions"].extend(session_actions)
        actions.extend(session_actions)

    if not left_off:
        left_off = [
            "No prior transcript or email context was found for the visible attendees.",
            "Use the opening minute to establish the desired outcome and decision owner.",
        ]
    if not actions:
        actions = [
            {
                "owner": rc.USER_FIRST_NAME,
                "text": "Confirm the concrete next step, owner, and timing before the meeting ends.",
                "source": "Generated prep",
            }
        ]

    attendee_names = [a.display_name or a.email for a in meeting.attendees if a.email or a.display_name]
    # Say something about the meeting, not about Recorder. When synthesis runs it
    # replaces this outright; this is only the floor when we have nothing to say,
    # and an empty string is better than telling the user what the app is doing.
    if related_transcripts or related_emails:
        why = (
            f"Meeting with {', '.join(attendee_names[:3])}. "
            "Prior context below was matched from earlier conversations and email."
            if attendee_names
            else ""
        )
    elif attendee_names:
        why = (
            f"First tracked conversation with {', '.join(attendee_names[:3])} — "
            "no prior email or recorded history on file."
        )
    else:
        why = ""

    item = {
        "id": meeting_id(meeting),
        "title": meeting.title,
        "timeRange": time_range(meeting),
        "location": meeting.location,
        "calendarLink": meeting.html_link,
        "attendees": [attendee_payload(a) for a in meeting.attendees],
        "prepState": "Ready" if related_emails or related_transcripts else "Draft",
        "sourceState": "Generated",
        "prepSources": prep_sources("Generated"),
        "why": why,
        "leftOff": left_off[:5],
        "background": left_off[:5],
        "actions": actions[:6],
        "suggestedAsk": actions[:1],
        "talkingPoints": [
            "Confirm what changed since the last interaction.",
            "Anchor the discussion on one specific business outcome.",
            "Identify blockers, owner, and next step before the call ends.",
        ],
        "openQuestions": [
            "What decision or commitment should this meeting produce?",
            "Who owns the next step after the call?",
            "Is there prior context that should be corrected or updated?",
        ],
        "relatedTranscripts": related_transcripts[:8],
        "relatedEmails": related_emails[:8],
        "context": context[:12],
        EVIDENCE_KEY: evidence,
    }
    return item


def series_item(
    meeting: prep.Meeting,
    notmuch: Any,
    account: str,
    service: Any,
    calendar_id: str,
    logger: Any,
) -> dict[str, Any]:
    entry = meeting.series_entry or {}
    lookback = int(entry.get("transcript_lookback_days", 60))
    limit = int(entry.get("max_prior_transcripts", 4))
    transcript_dir = DATA_ROOT / "meeting_transcripts"

    transcripts: list[dict[str, str]] = []
    transcripts.extend(
        prep.gather_series_transcripts(
            transcript_dir, service, calendar_id, meeting, lookback, limit, logger
        )
    )
    transcripts.extend(
        prep.gather_recorder_context(
            prep.RECORDER_RECORDINGS_DIR,
            meeting,
            lookback,
            limit,
            logger,
            match_emails={a.email for a in meeting.attendees if a.email},
        )
    )
    transcripts.extend(
        prep.gather_gemini_notes_for_series(
            notmuch, meeting, account, lookback, limit, logger
        )
    )
    transcripts = prep.dedupe_series_transcripts(transcripts)

    evidence = meeting_evidence(meeting, "series")

    left_off: list[str] = []
    actions: list[dict[str, str]] = []
    related = []
    for item in transcripts:
        source = clean_line(item.get("file") or "Transcript")
        related.append(source)
        excerpt = item.get("excerpt") or ""
        if excerpt.strip() or item.get("decisions"):
            evidence["notes"].append(
                {
                    "source": source,
                    "date": item.get("date") or "",
                    "excerpt": excerpt.strip(),
                    "decisions": item.get("decisions") or [],
                }
            )
        for line in summary_sentences(excerpt, limit=2):
            if len(left_off) < 5:
                left_off.append(line)
        # Recorder sessions carry structured todos; Gemini notes don't.
        item_actions = actions_from_recorder_todos(
            item.get("todos") or [], source, limit=6
        ) or action_items_from_text(excerpt, source, limit=6)
        evidence["priorActions"].extend(item_actions)
        actions.extend(item_actions)

    if not left_off:
        left_off = [
            "No prior session transcript was found in the configured lookback window.",
            "Treat this as a fresh baseline and make sure owners are captured this time.",
        ]
    if not actions:
        actions = [
            {
                "owner": "Meeting owner",
                "text": "Capture decisions and action owners in a persistent tracker.",
                "source": "Generated prep",
            }
        ]

    participants = [a.display_name or a.email for a in meeting.attendees if a.email or a.display_name]

    item = {
        "id": meeting_id(meeting),
        "title": meeting.title,
        "timeRange": time_range(meeting),
        "location": meeting.location,
        "calendarLink": meeting.html_link,
        "attendees": [attendee_payload(a) for a in meeting.attendees],
        "prepState": "Ready" if transcripts else "Draft",
        "sourceState": "Generated",
        "prepSources": prep_sources("Generated"),
        "why": (
            "This is a recurring internal meeting. The useful prep is continuity: "
            "what changed, what is still open, and which owners need follow-up."
        ),
        "leftOff": left_off[:5],
        "background": left_off[:5],
        "actions": actions[:6],
        "suggestedAsk": actions[:1],
        "talkingPoints": [
            "Start with unresolved owners from the most recent prior session.",
            "Ask which deals, accounts, or partner motions changed status.",
            "Separate decisions needed today from status updates.",
            "Confirm where the written source of truth lives after the meeting.",
        ],
        "openQuestions": [
            "Which carryover item is still blocked?",
            "What changed since the prior session?",
            "What should be written down so next week's prep has continuity?",
        ],
        "relatedTranscripts": related[:8],
        "relatedEmails": [],
        "context": [
            f"Participants: {', '.join(participants)}" if participants else "Participants: unavailable",
            f"Calendar: {account}",
            "Prep sources: Recorder transcripts, Gemini Notes, notmuch",
        ],
        EVIDENCE_KEY: evidence,
    }
    return item


def load_response(
    account: str,
    include_skipped: bool = True,
    range_key: str = DEFAULT_RANGE,
    synthesize: bool = True,
) -> dict[str, Any]:
    logger = prep.setup_logging()
    prep.load_project_env(rc.ENV_FILE)
    service = prep.load_calendar_service(prep.CONFIG_DIR / "token.json")
    if service is None:
        raise RuntimeError(f"No usable calendar token at {prep.CONFIG_DIR / 'token.json'}")
    calendar_id = prep.resolve_calendar_id(service, account)
    if not calendar_id:
        raise RuntimeError(f"Could not resolve calendar for account={account}")

    events = fetch_events_in_range(service, calendar_id, range_key)
    my_email = prep.ACCOUNT_MY_EMAIL.get(account, "")
    internal_allowlist = prep.load_internal_meetings_allowlist(logger)
    meetings = prep.build_meetings(
        events,
        my_email=my_email,
        logger=logger,
        internal_allowlist=internal_allowlist,
    )
    # Routed per account (Gmail API or notmuch) by EMAIL_SEARCH_BACKEND[_<ACCOUNT>].
    from email_search import make_email_search

    notmuch = make_email_search(default_account=account)

    items: list[dict[str, Any]] = []
    # Items with no daily brief get their narrative from Bedrock. A
    # brief, when present, is the higher-quality source and wins — so we skip
    # synthesis for those rather than paying for a call we'd discard.
    pending: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for meeting in meetings:
        if meeting.series_entry is not None:
            item = series_item(meeting, notmuch, account, service, calendar_id, logger)
        else:
            item = external_item(meeting, notmuch, account, logger)
        brief = find_brief_for_meeting(meeting)
        if brief is not None:
            apply_brief(item, brief)
        elif item.get(EVIDENCE_KEY):
            pending.append((item, item[EVIDENCE_KEY]))
        item.update(day_fields(meeting.start.astimezone()))
        items.append(item)

    if pending and synthesize:
        count = prep_synthesis.synthesize_items(pending)
        logger.info("Synthesized %d/%d brief(s) via Bedrock", count, len(pending))

    for item in items:
        item.pop(EVIDENCE_KEY, None)

    skipped: list[dict[str, Any]] = []
    for event in events:
        reason = event_skip_reason(event, my_email, internal_allowlist)
        if reason is None:
            continue
        item = skipped_item(event, reason)
        start_local = event_start_local(event)
        if start_local is not None:
            item.update(day_fields(start_local))
        skipped.append(item)

    generated_at = datetime.now().astimezone().isoformat()
    response = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": generated_at,
        "calendarId": calendar_id,
        "account": account,
        "range": range_key,
        "items": items,
        "skippedItems": skipped if include_skipped else [],
        "diagnostics": diagnostics_for_events(events, my_email, internal_allowlist),
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path(account, range_key).write_text(
        json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return response


def load_calendar_diagnostics(account: str, range_key: str = DEFAULT_RANGE) -> dict[str, Any]:
    logger = prep.setup_logging()
    prep.load_project_env(rc.ENV_FILE)
    service = prep.load_calendar_service(prep.CONFIG_DIR / "token.json")
    if service is None:
        raise RuntimeError(f"No usable calendar token at {prep.CONFIG_DIR / 'token.json'}")
    calendar_id = prep.resolve_calendar_id(service, account)
    if not calendar_id:
        raise RuntimeError(f"Could not resolve calendar for account={account}")
    events = fetch_events_in_range(service, calendar_id, range_key)
    internal_allowlist = prep.load_internal_meetings_allowlist(logger)
    my_email = prep.ACCOUNT_MY_EMAIL.get(account, "")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": datetime.now().astimezone().isoformat(),
        "calendarId": calendar_id,
        "account": account,
        "diagnostics": diagnostics_for_events(events, my_email, internal_allowlist),
    }


def allowlist_title(title: str) -> None:
    path = prep.INTERNAL_MEETINGS_FILE
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    pattern = f"(?i)^{re.escape(title)}$"
    for entry in entries:
        if entry.get("title_pattern") == pattern:
            return
    entries.append(
        {
            "title_pattern": pattern,
            "recurring_event_id": None,
            "transcript_lookback_days": 60,
            "max_prior_transcripts": 4,
        }
    )
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_cached_response(
    account: str, error: str, range_key: str = DEFAULT_RANGE
) -> dict[str, Any] | None:
    path = cache_path(account, range_key)
    if not path.exists():
        return None
    cached = json.loads(path.read_text(encoding="utf-8"))
    cached["stale"] = True
    cached["error"] = error
    return cached


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", default=rc.DEFAULT_ACCOUNT)
    parser.add_argument("--event-id", default="", help="Return one prep item")
    parser.add_argument("--debug-calendar", action="store_true", help="Print raw calendar eligibility diagnostics")
    parser.add_argument("--no-cache", action="store_true", help="Do not fall back to cached prep JSON on failure")
    parser.add_argument("--allowlist-title", default="", help="Add an exact-title internal meeting allowlist entry")
    parser.add_argument(
        "--range",
        default=DEFAULT_RANGE,
        choices=VALID_RANGES,
        help="Lookahead window for upcoming events (default: today)",
    )
    parser.add_argument(
        "--synthesize",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Write brief narrative with Bedrock for meetings with no daily "
        "brief (default: on). --no-synthesize returns structured prep only.",
    )
    args = parser.parse_args()

    try:
        if args.allowlist_title:
            allowlist_title(args.allowlist_title)
            json.dump({"ok": True, "title": args.allowlist_title}, sys.stdout, ensure_ascii=False)
            sys.stdout.write("\n")
            return 0
        if args.debug_calendar:
            with contextlib.redirect_stdout(sys.stderr):
                response = load_calendar_diagnostics(args.account.lower(), args.range)
            json.dump(response["diagnostics"], sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
            return 0
        with contextlib.redirect_stdout(sys.stderr):
            response = load_response(
                args.account.lower(), range_key=args.range, synthesize=args.synthesize
            )
        if args.event_id:
            items = response.get("items", [])
            items = [item for item in items if item.get("id") == args.event_id]
            json.dump(items[0] if items else {}, sys.stdout, ensure_ascii=False)
        else:
            json.dump(response, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    except Exception as exc:
        message = f"fetch_meeting_prep failed: {type(exc).__name__}: {exc}"
        print(message, file=sys.stderr)
        cached = None if args.no_cache else load_cached_response(args.account.lower(), message, args.range)
        if cached is not None:
            json.dump(cached, sys.stdout, ensure_ascii=False)
        else:
            json.dump({
                "schemaVersion": SCHEMA_VERSION,
                "generatedAt": datetime.now().astimezone().isoformat(),
                "account": args.account.lower(),
                "range": args.range,
                "items": [],
                "skippedItems": [],
                "diagnostics": [],
                "error": message,
            }, sys.stdout)
        sys.stdout.write("\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
