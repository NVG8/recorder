"""Extract todos / action items from a transcript using AWS Bedrock (Claude).

Usage: uv run extract_todos.py <transcript_json_path>
  transcript_json_path: file produced by transcribe.py, or a plain .txt transcript

Env:
  BEDROCK_REGION       (default: us-east-1)
  BEDROCK_MODEL_ID     (default: us.anthropic.claude-sonnet-4-5-20250929-v1:0)
  RECORDER_USER_EMAIL  who "mine" todos belong to (or user_email in the
  RECORDER_USER_NAME   recorder config, see recorder_config.py)
  AWS creds via the usual chain (env, profile, instance role…)

Output (stdout): JSON
  {"summary": str,
   "todos": [{"text": str, "owner": str|null, "bucket": "mine"|"waiting_on"|"fyi",
              "due": str|null, "due_iso": str|null, "context": str}, ...],
   "resolved_prior": [{"id": str, "reason": str}, ...]}

Backward compatible: older consumers that only read `text/owner/due/context` and
`summary/todos` keep working — the new fields are additive.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import boto3

import recorder_config as rc


DEFAULT_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
DEFAULT_MODEL = os.environ.get(
    "BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)
DEFAULT_USER_EMAIL = rc.USER_EMAIL
DEFAULT_USER_NAME = rc.USER_NAME
# Cheap, fast model for the second-pass actionability filter.
DEFAULT_FILTER_MODEL = os.environ.get(
    "BEDROCK_FILTER_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)

SYSTEM_PROMPT = """You turn a meeting / call transcript into a clean, deduplicated action list for one specific user.

THE USER
The user is {user_name} <{user_email}>. The "Who is who" block in the message names the user and every other attendee. In the transcript people are usually addressed by first name only — if another attendee shares the user's first name, DO NOT assume that name means the user; attribute by who the surrounding context and email/role point to.
{speaker_guidance}
First decide `owner` — the person who will PERFORM the action. CRITICAL: the owner is the performer, NOT the person being addressed. "I'll put thoughts together, Sam" means the SPEAKER owns it (Sam is just being addressed); "Sam, can you send that over" means SAM owns it. Decide owner from who does the verb, not who is named.

Then answer two precise yes/no questions instead of guessing a label:
- `owner_is_user`: Is the `owner` you just decided the user themselves? true only when the user is unambiguously the performer — they said "I'll…"/"I can…", or were explicitly handed the task ("<user's name>, can you…"). If the user is merely addressed, cc'd, mentioned, or it's unclear which same-named person is meant, this is FALSE.
- `user_is_awaiting`: Is this a deliverable owed TO the user that blocks them until it arrives? true only if the user is the explicit recipient/beneficiary. Merely being copied or present is NOT awaiting.
Both default to false. Prefer false when unsure — a short, trustworthy "mine" list beats an inflated one. (The app derives the mine/waiting/fyi grouping from these two answers; you do not pick it.)

WHAT COUNTS AS AN ACTION
Include an item ONLY if it names a concrete next step someone must take. Every `text` must start with an imperative verb and reference a specific object.
- GOOD: "Send Dan the Q3 pipeline numbers", "Schedule the Acme follow-up call for June 4", "Review the Globex contract redlines".
- NOT ACTIONS — exclude these entirely: topic summaries ("Provide context on the five deals"), vague intentions ("Develop thoughts on the webinar concept"), anything already done during the call, hypotheticals ("we could maybe…"), and restatements of what was discussed.
When in doubt, leave it out. A short, true list beats a long, noisy one.

DUE DATES
The meeting happened on {meeting_date}. If a due date or timeframe is stated, put the verbatim phrase in `due` and the resolved calendar date (YYYY-MM-DD) in `due_iso` when you can work it out from the meeting date; otherwise `due_iso` is null. Keep words like "tentative" in `due`.

OWNERS & NAMES
If a "Meeting context" section is provided, use it to fix mistranscribed attendee names (the speech-to-text model garbles uncommon names) and prefer the attendee list for owner attribution, using full names. Ground the summary in the meeting title / description.

CONTEXT
`context` is a short phrase (≤120 chars) quoting or paraphrasing the part of the transcript the action came from.

DECISIONS
A decision is a settled choice the group landed on — a direction chosen, an option ruled out, a number or date agreed, an approach committed to. Record what was decided, not what was discussed.
- GOOD: "Running the pilot in the EU region rather than waiting for US capacity", "Holding the renewal at a 6% uplift if the API seat pack is included", "Dropping the nonprofit segment to focus campaigns on manufacturing".
- NOT DECISIONS — exclude: open questions, things deferred to a later meeting, one person's opinion that nobody agreed to, and anything that is really an action item (those belong in `todos`).
Each entry gets `text` (the decision, one sentence) and `context` (≤120 chars from the transcript showing where it was settled). Empty list when nothing was actually decided — that is the common case for status meetings, and an empty list is far better than a restated agenda.

DEDUPLICATION AGAINST PRIOR OPEN ITEMS
{dedup_rules}

OUTPUT
Respond with ONLY a JSON object, no prose:
{{"summary": "2-3 sentence summary of the conversation",
  "todos": [{{"text": "...", "owner": "..."|null, "owner_is_user": false, "user_is_awaiting": false, "due": "..."|null, "due_iso": "YYYY-MM-DD"|null, "context": "..."}}],
  "decisions": [{{"text": "...", "context": "..."}}],
  "resolved_prior": [{{"id": "...", "reason": "..."}}]}}
"""

# Injected into SYSTEM_PROMPT only when the transcript is speaker-labeled (see
# transcribe.py --mic). Turns owner/user attribution from a guess into a read.
_SPEAKER_GUIDANCE = """
SPEAKER LABELS (authoritative — use them)
This transcript is speaker-labeled. Every line beginning "You:" was spoken by the USER ({user_name}); every line beginning "Remote:" was spoken by some OTHER attendee — NEVER the user. Decide owner and the two booleans from this:
- A first-person commitment on a "You:" line ("I'll…", "I can…", "let me…") → the USER performs it: owner_is_user = true.
- A commitment on a "Remote:" line → another attendee performs it: owner_is_user = false; if that deliverable is owed to the user, user_is_awaiting = true.
- A task handed to the user on a "Remote:" line ("<user's name>, can you…") → the USER performs it: owner_is_user = true.
For a "Remote:" owner, still use the attendee list to name WHICH attendee. These labels override first-name guessing and the name-collision caveat.
"""

_DEDUP_NONE = (
    "No prior items were provided. Set `resolved_prior` to an empty list and put every "
    "qualifying action in `todos`."
)
_DEDUP_PRIORS = (
    "You are given the still-open action items from earlier meetings in this recurring series "
    "(see the \"Prior open items\" section). Decide the fate of each prior item, and what is new:\n"
    "- If a prior item is mentioned again but is NOT finished (still in progress, re-discussed, "
    "restated, or just carried over): do NOTHING with it — do not add it to `todos` (that would "
    "duplicate it) and do not add it to `resolved_prior`. It stays open and tracked as-is.\n"
    "- Add an id to `resolved_prior` ONLY when the meeting shows the prior item was actually "
    "COMPLETED — give a one-phrase `reason` stating the completion. NEVER resolve an item because "
    "it is a duplicate, was re-discussed, or because someone WILL do it. \"Duplicate\" and future "
    "intent are not completion.\n"
    "- Put in `todos` ONLY genuinely new actions that do not match any prior open item."
)


FILTER_SYSTEM_PROMPT = """You are a strict reviewer of candidate action items pulled from a meeting transcript for {user_name} <{user_email}>. Keep ONLY genuine, concrete, still-open next steps.

DROP an item if it is any of:
- a vague intention or aspiration ("develop thoughts on X", "explore ideas", "consider Y")
- a summary or restatement of what was discussed rather than a step someone will take
- hypothetical ("we could maybe…", "it might make sense to…")
- already done during the meeting
- a near-duplicate of another item (keep the single clearest one)

You MAY tighten wording so each kept item is a crisp imperative (verb + specific object). DO NOT invent new items. DO NOT change `owner`, `bucket`, `due`, `due_iso`, or `context` except to fix an obvious transcription error — pass each kept item's `bucket` through UNCHANGED (it was already decided upstream).

Return ONLY a JSON object, no prose:
{{"todos": [{{"text": "...", "owner": "..."|null, "bucket": "mine"|"waiting_on"|"fyi", "due": "..."|null, "due_iso": "YYYY-MM-DD"|null, "context": "..."}}]}}
"""


def refine_todos(
    transcript: str,
    todos: list[dict],
    region: str,
    filter_model: str,
    user_name: str,
    user_email: str,
    event: dict | None = None,
) -> list[dict]:
    """Second pass: drop soft/duplicate non-actions. Falls back to the input on any failure."""
    if not todos:
        return todos
    client = boto3.client("bedrock-runtime", region_name=region)
    candidates = json.dumps({"todos": todos}, ensure_ascii=False, indent=2)
    user_text = (
        f"{format_event_context(event)}"
        f"Candidate items:\n{candidates}\n\n"
        f"Transcript:\n\n{transcript}\n\nReturn JSON only."
    )
    try:
        resp = client.converse(
            modelId=filter_model,
            system=[{"text": FILTER_SYSTEM_PROMPT.format(user_name=user_name, user_email=user_email)}],
            messages=[{"role": "user", "content": [{"text": user_text}]}],
            inferenceConfig={"maxTokens": 2048, "temperature": 0.0},
        )
        text = "".join(p.get("text", "") for p in resp["output"]["message"]["content"]).strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].lstrip()
        kept = _coerce_result(json.loads(text))["todos"]
        # Guard against a degenerate pass that drops everything.
        if not kept:
            print("filter pass returned no items; keeping unfiltered", file=sys.stderr)
            return todos
        print(f"filter pass: {len(todos)} → {len(kept)}", file=sys.stderr)
        return kept
    except Exception as e:  # noqa: BLE001 — never let the filter lose data
        print(f"filter pass failed ({e}); keeping unfiltered", file=sys.stderr)
        return todos


_SPEAKER_LINE_RE = re.compile(r"^(?:You|Remote):\s", re.MULTILINE)


def transcript_is_diarized(transcript: str) -> bool:
    """True when the transcript carries `You:` / `Remote:` speaker labels."""
    return len(_SPEAKER_LINE_RE.findall(transcript or "")) >= 2


def build_system_prompt(
    user_name: str, user_email: str, meeting_date: str, has_priors: bool, diarized: bool = False
) -> str:
    guidance = _SPEAKER_GUIDANCE.format(user_name=user_name) if diarized else ""
    return SYSTEM_PROMPT.format(
        user_name=user_name,
        user_email=user_email,
        meeting_date=meeting_date or "unknown",
        dedup_rules=_DEDUP_PRIORS if has_priors else _DEDUP_NONE,
        speaker_guidance=guidance,
    )


def resolve_user_identity(event: dict | None, arg_email: str | None, arg_name: str | None) -> tuple[str, str]:
    """Pick the 'mine' identity: explicit args > self attendee > env defaults."""
    if arg_email:
        return (arg_name or DEFAULT_USER_NAME or arg_email), arg_email
    for a in (event or {}).get("attendees") or []:
        if a.get("self"):
            # The calendar "self" entry often has no display name; fall back to the
            # configured name so the model has a human name to match in the transcript.
            name = a.get("name") or arg_name or DEFAULT_USER_NAME
            return name, (a.get("email") or DEFAULT_USER_EMAIL)
    return (arg_name or DEFAULT_USER_NAME), DEFAULT_USER_EMAIL


def meeting_date_from_event(event: dict | None) -> str:
    start = (event or {}).get("start") or ""
    # ISO timestamps lead with YYYY-MM-DD; keep just the date for the prompt.
    return start[:10] if len(start) >= 10 else ""


def format_identity_block(event: dict | None, user_name: str, user_email: str) -> str:
    """High-salience 'who is who' block: pins the user and flags first-name collisions."""
    lines = ["Who is who:", f"  USER (the person these todos are for): {user_name} <{user_email}>"]
    user_first = (user_name or "").split()[0].lower() if user_name else ""
    others = []
    collision = False
    for a in (event or {}).get("attendees") or []:
        if a.get("self"):
            continue
        name = a.get("name") or ""
        email = a.get("email") or ""
        if not (name or email):
            continue
        label = f"{name} <{email}>" if name and email else (name or email)
        others.append(f"  OTHER: {label}")
        if user_first and name and name.split()[0].lower() == user_first:
            collision = True
    lines.extend(others)
    if collision:
        lines.append(
            f"  ⚠ NAME COLLISION: another attendee shares the first name \"{user_name.split()[0]}\". "
            f"\"{user_name.split()[0]}\" in the transcript may refer to either — only treat it as the "
            f"USER when context makes that unambiguous."
        )
    return "\n".join(lines) + "\n\n"


def format_event_context(event: dict | None) -> str:
    """Render an event JSON dict into a brief prompt-friendly context block."""
    if not event:
        return ""
    lines = ["Meeting context (from calendar):"]
    if event.get("title"):
        lines.append(f"  Title: {event['title']}")
    if event.get("start") and event.get("end"):
        lines.append(f"  Time:  {event['start']} → {event['end']}")
    attendees = event.get("attendees") or []
    if attendees:
        labels = []
        for a in attendees:
            name = a.get("name") or ""
            email = a.get("email") or ""
            if name and email:
                labels.append(f"{name} <{email}>")
            else:
                labels.append(name or email)
        lines.append(f"  Attendees: {', '.join(labels)}")
    desc = (event.get("description") or "").strip()
    if desc:
        # Truncate noisy HTML-y descriptions.
        if len(desc) > 600:
            desc = desc[:600] + "…"
        lines.append(f"  Description: {desc}")
    return "\n".join(lines) + "\n\n"


def format_prior_todos(priors: list[dict]) -> str:
    """Render prior still-open todos (from earlier meetings in the series)."""
    if not priors:
        return ""
    lines = ["Prior open items (from earlier meetings in this series):"]
    for p in priors:
        pid = p.get("id") or ""
        text = (p.get("text") or "").strip()
        owner = p.get("owner")
        when = p.get("meeting_date") or p.get("meeting_title") or ""
        tail = []
        if owner:
            tail.append(f"owner={owner}")
        if when:
            tail.append(f"from={when}")
        suffix = f"  ({'; '.join(tail)})" if tail else ""
        lines.append(f"  [{pid}] {text}{suffix}")
    return "\n".join(lines) + "\n\n"


def load_transcript(path: Path) -> str:
    raw = path.read_text(encoding="utf-8").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return str(data.get("text", "")).strip() or raw
        except json.JSONDecodeError:
            return raw
    return raw


def load_priors(path: Path | None) -> list[dict]:
    if not path or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"could not parse prior-todos file: {path}", file=sys.stderr)
        return []
    # Accept either a bare list or {"todos": [...]}.
    if isinstance(data, dict):
        data = data.get("todos") or data.get("priors") or []
    return [p for p in data if isinstance(p, dict) and (p.get("text") or "").strip()]


def _derive_bucket(t: dict) -> str:
    """Bucket is derived from two sharp booleans, not picked directly by the model.

    Falls back to a directly-supplied `bucket` (e.g. from the filter pass, which
    preserves it) when the booleans are absent.
    """
    if "owner_is_user" in t or "user_is_awaiting" in t:
        if bool(t.get("owner_is_user")):
            return "mine"
        if bool(t.get("user_is_awaiting")):
            return "waiting_on"
        return "fyi"
    bucket = t.get("bucket")
    return bucket if bucket in ("mine", "waiting_on", "fyi") else "fyi"


def _coerce_result(parsed: dict) -> dict:
    """Ensure the result has the expected keys with sane defaults."""
    todos = []
    for t in parsed.get("todos") or []:
        if not isinstance(t, dict):
            continue
        todos.append(
            {
                "text": t.get("text") or "",
                "owner": t.get("owner"),
                "bucket": _derive_bucket(t),
                "due": t.get("due"),
                "due_iso": t.get("due_iso"),
                "context": t.get("context") or "",
            }
        )
    decisions = [
        {"text": (d.get("text") or "").strip(), "context": (d.get("context") or "").strip()}
        for d in (parsed.get("decisions") or [])
        if isinstance(d, dict) and (d.get("text") or "").strip()
    ]
    resolved = [
        {"id": r.get("id"), "reason": r.get("reason") or ""}
        for r in (parsed.get("resolved_prior") or [])
        if isinstance(r, dict) and r.get("id") and not _reason_implies_open(r.get("reason"))
    ]
    return {
        "summary": parsed.get("summary") or "",
        "todos": todos,
        "decisions": decisions,
        "resolved_prior": resolved,
    }


# Phrases in a "resolved" reason that actually mean the item is still open — the
# model occasionally resolves something while admitting it isn't done. Drop those.
_OPEN_MARKERS = (
    "not yet", "not completed", "not done", "not finished", "incomplete",
    "still in progress", "in progress", "still pending", "pending", "hasn't",
    "has not", "haven't", "have not", "won't", "will not", "to be ", "yet to",
    "ongoing", "not resolved", "unresolved", "still need", "still needs",
    # A re-discussed/restated item is NOT a completion — it's still open.
    "duplicate", "re-discussed", "rediscussed", "restated", "reiterated",
    "mentioned again", "carried over", "carried forward", "follow up needed",
    # Future intent (" will <verb>", "going to …", "plans to …") is not done.
    # Bare " will " (surrounding spaces) catches any future verb without
    # enumerating them, while avoiding "goodwill"/"willing".
    " will ", "going to", "plans to", "intends to", "agreed to", "needs to",
)


def _reason_implies_open(reason: str | None) -> bool:
    low = (reason or "").lower()
    return any(m in low for m in _OPEN_MARKERS)


def extract(
    transcript: str,
    region: str,
    model_id: str,
    event: dict | None = None,
    priors: list[dict] | None = None,
    user_email: str | None = None,
    user_name: str | None = None,
    refine: bool = True,
    filter_model: str = DEFAULT_FILTER_MODEL,
) -> dict:
    priors = priors or []
    user_name_r, user_email_r = resolve_user_identity(event, user_email, user_name)
    diarized = transcript_is_diarized(transcript)
    if diarized:
        print("transcript is speaker-labeled; using You/Remote attribution", file=sys.stderr)
    system_prompt = build_system_prompt(
        user_name_r, user_email_r, meeting_date_from_event(event),
        has_priors=bool(priors), diarized=diarized,
    )

    client = boto3.client("bedrock-runtime", region_name=region)
    user_text = (
        f"{format_identity_block(event, user_name_r, user_email_r)}"
        f"{format_event_context(event)}"
        f"{format_prior_todos(priors)}"
        f"Transcript:\n\n{transcript}\n\nReturn JSON only."
    )
    resp = client.converse(
        modelId=model_id,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_text}]}],
        inferenceConfig={"maxTokens": 4096, "temperature": 0.0},
    )
    parts = resp["output"]["message"]["content"]
    text = "".join(p.get("text", "") for p in parts).strip()
    # Be lenient: strip code fences if the model added them.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    try:
        result = _coerce_result(json.loads(text))
    except json.JSONDecodeError as e:
        print(f"model returned non-JSON: {e}\n---\n{text}", file=sys.stderr)
        return {"summary": "", "todos": [], "decisions": [], "resolved_prior": [], "raw": text}

    if refine:
        result["todos"] = refine_todos(
            transcript, result["todos"], region, filter_model,
            user_name_r, user_email_r, event=event,
        )
    return result


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("transcript", type=Path)
    p.add_argument("--event", type=Path, default=None,
                   help="JSON file produced by fetch_meeting.py for prompt enrichment.")
    p.add_argument("--prior-todos", type=Path, default=None,
                   help="JSON list of still-open todos from earlier meetings in this series "
                        "([{id,text,owner,due,meeting_date,meeting_title}]). Enables dedup + "
                        "resolved_prior output.")
    p.add_argument("--user-email", default=None, help="Override the 'mine' identity email.")
    p.add_argument("--user-name", default=None, help="Override the 'mine' identity name.")
    p.add_argument("--region", default=DEFAULT_REGION)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--filter-model", default=DEFAULT_FILTER_MODEL,
                   help="Model for the second-pass actionability filter (default: Haiku 4.5).")
    p.add_argument("--refine", action=argparse.BooleanOptionalAction, default=True,
                   help="Run the second-pass filter to drop soft/duplicate non-actions (default: on).")
    args = p.parse_args()

    if not args.transcript.exists():
        print(f"transcript not found: {args.transcript}", file=sys.stderr)
        return 2

    text = load_transcript(args.transcript)
    if not text:
        print("empty transcript", file=sys.stderr)
        return 3

    event: dict | None = None
    if args.event and args.event.exists():
        try:
            ev = json.loads(args.event.read_text())
            if isinstance(ev, dict) and ev.get("title"):
                event = ev
                print(f"using meeting context: {ev.get('title')}", file=sys.stderr)
        except json.JSONDecodeError:
            print(f"could not parse event file: {args.event}", file=sys.stderr)

    priors = load_priors(args.prior_todos)
    if priors:
        print(f"reconciling against {len(priors)} prior open item(s)", file=sys.stderr)

    print(f"calling bedrock {args.model} ({args.region})…", file=sys.stderr, flush=True)
    result = extract(
        text, args.region, args.model,
        event=event, priors=priors,
        user_email=args.user_email, user_name=args.user_name,
        refine=args.refine, filter_model=args.filter_model,
    )
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
