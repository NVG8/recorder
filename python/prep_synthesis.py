"""Bedrock-backed narrative synthesis for Recorder meeting prep.

`fetch_meeting_prep.py` gathers the structured half of a brief deterministically
— calendar, notmuch email history, Gemini notes, prior Recorder sessions. This
module turns that raw evidence into the narrative sections the prep pane shows:
why the meeting matters, background, talking points, open questions, and
suggested asks.

Runs on Bedrock via boto3 `converse`, matching `extract_todos.py`, so prep works
without a local inference server on the network.

Design notes:
  * Soft-fails. Any Bedrock or parse error returns None and the caller keeps its
    deterministic prep. The pane must never go blank because a model call failed.
  * Cached by evidence hash under ~/Library/Application Support/Recorder/
    prep-synthesis/. Refreshing the pane re-spends tokens only when the
    underlying evidence actually changed.
  * Runs meetings concurrently — the UI waits on this call.

Env:
  BEDROCK_PREP_MODEL_ID   (default: us.anthropic.claude-sonnet-4-5-20250929-v1:0)
  BEDROCK_REGION          (default: us-east-1)
  RECORDER_PREP_SYNTHESIS set to 0 to disable synthesis entirely
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import recorder_config as rc

# Bump when the prompt changes so cached results are recomputed.
PROMPT_VERSION = 2

DEFAULT_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
DEFAULT_MODEL = os.environ.get(
    "BEDROCK_PREP_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)

CACHE_DIR = Path.home() / "Library" / "Application Support" / "Recorder" / "prep-synthesis"

MAX_WORKERS = 4


def enabled() -> bool:
    return os.environ.get("RECORDER_PREP_SYNTHESIS", "1").strip() not in ("0", "false", "no")


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You write pre-meeting briefs for {user}, who runs partner and customer \
meetings. You are given everything the app could gather about one upcoming \
meeting: the calendar entry, the attendees, recent email threads, notes from \
past meetings, and transcript excerpts from prior sessions with these people.

Write the brief they'd want to read in the ninety seconds before the call.

Rules:
- Ground every claim in the supplied evidence. If the evidence says a deal is \
stalled on pricing, say that. If there is no evidence for something, leave it out.
- Never invent history, names, numbers, dates, or commitments. When there is \
little or no prior context, say so plainly and write prep that works from the \
meeting title, description, and attendee list alone.
- Be specific to this meeting. Generic advice ("confirm next steps") is worse \
than nothing — drop a section rather than pad it.
- Lines marked DECIDED are settled choices from earlier sessions. Treat them as \
established, not open: surface one when it needs following through or has since \
been contradicted, never as something still to be agreed.
- Refer to people by name. Write in plain sentences, no marketing tone.

Return JSON only, matching this shape exactly:

{
  "why": "One or two sentences on what this meeting is and why it matters now.",
  "background": ["What happened previously that's relevant. 0-6 items."],
  "talkingPoints": ["What {user} should raise or steer toward. 0-6 items."],
  "openQuestions": ["What they don't know and should ask. 0-5 items."],
  "suggestedAsks": ["Concrete asks or next steps to land. 0-4 items."]
}

Each list item is one plain sentence, no leading bullet or dash. Use an empty \
list when the evidence doesn't support that section. Return JSON only — no \
prose, no code fences.
"""


def _trunc(value: str, limit: int) -> str:
    value = (value or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def format_evidence(evidence: dict[str, Any]) -> str:
    """Render the gathered context as the user turn."""
    lines: list[str] = []

    lines.append("# Meeting")
    lines.append(f"Title: {evidence.get('title') or '(no title)'}")
    if evidence.get("when"):
        lines.append(f"When: {evidence['when']}")
    if evidence.get("durationMinutes"):
        lines.append(f"Duration: {evidence['durationMinutes']} minutes")
    if evidence.get("location"):
        lines.append(f"Location: {evidence['location']}")
    kind = evidence.get("kind") or "external"
    lines.append(
        "Type: recurring internal series" if kind == "series" else "Type: external meeting"
    )
    if evidence.get("description"):
        lines.append(f"Description: {_trunc(evidence['description'], 800)}")
    lines.append("")

    attendees = evidence.get("attendees") or []
    if attendees:
        lines.append("# Attendees")
        for att in attendees:
            bits = [att.get("name") or att.get("email") or "unknown"]
            if att.get("email") and att.get("email") != bits[0]:
                bits.append(att["email"])
            if att.get("company"):
                bits.append(f"@ {att['company']}")
            lines.append(f"- {' — '.join(bits)}")
        lines.append("")

    emails = evidence.get("emails") or []
    if emails:
        lines.append("# Recent email")
        for item in emails:
            header = item.get("subject") or "(no subject)"
            if item.get("date"):
                header = f"{header}  [{item['date']}]"
            lines.append(f"## {header}")
            lines.append(_trunc(item.get("excerpt") or "", 1200))
            lines.append("")

    notes = evidence.get("notes") or []
    if notes:
        lines.append("# Notes and transcripts from prior meetings")
        for item in notes:
            header = item.get("source") or "Prior session"
            if item.get("date"):
                header = f"{header}  [{item['date']}]"
            lines.append(f"## {header}")
            for decision in item.get("decisions") or []:
                lines.append(f"DECIDED: {decision}")
            lines.append(_trunc(item.get("excerpt") or "", 1500))
            lines.append("")

    priors = evidence.get("priorActions") or []
    if priors:
        lines.append("# Open items from prior meetings")
        for item in priors:
            owner = item.get("owner") or "unassigned"
            lines.append(f"- [{owner}] {_trunc(item.get('text') or '', 260)}")
        lines.append("")

    if not emails and not notes and not priors:
        lines.append(
            "# Note\nNo prior email, transcript, or meeting-note context was found for "
            "these attendees. Ground the brief in the title, description, and attendee "
            "list only, and say plainly that there is no prior history on file."
        )
        lines.append("")

    lines.append("Return JSON only.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _cache_key(evidence: dict[str, Any], model_id: str) -> str:
    payload = json.dumps(
        {"v": PROMPT_VERSION, "model": model_id, "evidence": evidence},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_path(meeting_id: str) -> Path:
    safe = hashlib.sha256(meeting_id.encode("utf-8")).hexdigest()[:32]
    return CACHE_DIR / f"{safe}.json"


def _cache_read(meeting_id: str, key: str) -> dict[str, Any] | None:
    path = _cache_path(meeting_id)
    if not path.exists():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if cached.get("key") != key:
        return None
    result = cached.get("result")
    return result if isinstance(result, dict) else None


def _cache_write(meeting_id: str, key: str, result: dict[str, Any]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(meeting_id).write_text(
            json.dumps(
                {
                    "key": key,
                    "generatedAt": datetime.now().astimezone().isoformat(),
                    "result": result,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"prep synthesis: cache write failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Bedrock
# ---------------------------------------------------------------------------


def _coerce(raw: Any) -> dict[str, Any]:
    """Normalize the model's JSON into the shape the caller expects."""
    if not isinstance(raw, dict):
        raise ValueError("expected a JSON object")

    def as_list(value: Any, limit: int) -> list[str]:
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for entry in value:
            if isinstance(entry, str):
                text = entry.strip().lstrip("-•").strip()
                if text:
                    out.append(text)
            if len(out) >= limit:
                break
        return out

    why = raw.get("why")
    return {
        "why": why.strip() if isinstance(why, str) else "",
        "background": as_list(raw.get("background"), 6),
        "talkingPoints": as_list(raw.get("talkingPoints"), 6),
        "openQuestions": as_list(raw.get("openQuestions"), 5),
        "suggestedAsks": as_list(raw.get("suggestedAsks"), 4),
    }


def _invoke(client: Any, model_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
    resp = client.converse(
        modelId=model_id,
        system=[{"text": SYSTEM_PROMPT.replace("{user}", rc.USER_FIRST_NAME)}],
        messages=[{"role": "user", "content": [{"text": format_evidence(evidence)}]}],
        inferenceConfig={"maxTokens": 2048, "temperature": 0.2},
    )
    parts = resp["output"]["message"]["content"]
    text = "".join(p.get("text", "") for p in parts).strip()
    # Be lenient: strip code fences if the model added them.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    return _coerce(json.loads(text))


def synthesize_one(
    meeting_id: str,
    evidence: dict[str, Any],
    client: Any,
    model_id: str,
) -> dict[str, Any] | None:
    """Synthesize one brief. Returns None on any failure — never raises."""
    key = _cache_key(evidence, model_id)
    cached = _cache_read(meeting_id, key)
    if cached is not None:
        return cached
    try:
        result = _invoke(client, model_id, evidence)
    except Exception as exc:  # noqa: BLE001 - soft-fail by design
        print(
            f"prep synthesis failed for {meeting_id}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None
    _cache_write(meeting_id, key, result)
    return result


def apply_synthesis(item: dict[str, Any], result: dict[str, Any]) -> None:
    """Overlay synthesized narrative onto a generated prep item.

    Mirrors `apply_brief` in fetch_meeting_prep.py. Only non-empty sections
    replace the deterministic defaults, so a partial result still improves the
    pane instead of blanking it.
    """
    if result.get("why"):
        item["why"] = result["why"]
    if result.get("background"):
        item["leftOff"] = result["background"]
        item["background"] = result["background"]
    if result.get("talkingPoints"):
        item["talkingPoints"] = result["talkingPoints"]
    if result.get("openQuestions"):
        item["openQuestions"] = result["openQuestions"]
    if result.get("suggestedAsks"):
        actions = [
            {"owner": rc.USER_FIRST_NAME, "text": ask, "source": "Suggested ask"}
            for ask in result["suggestedAsks"]
        ]
        item["actions"] = actions
        item["suggestedAsk"] = actions
    item["prepState"] = "Ready"
    item["sourceState"] = "Synthesized"
    sources = item.get("prepSources") or []
    item["prepSources"] = ["Bedrock synthesis", *sources]


def synthesize_items(
    pending: list[tuple[dict[str, Any], dict[str, Any]]],
    region: str = DEFAULT_REGION,
    model_id: str = DEFAULT_MODEL,
) -> int:
    """Synthesize and apply narrative for each (item, evidence) pair.

    Returns the number of items successfully synthesized. Soft-fails as a whole:
    if the Bedrock client can't even be constructed, every item keeps its
    deterministic prep.
    """
    if not pending or not enabled():
        return 0

    try:
        import boto3

        client = boto3.client("bedrock-runtime", region_name=region)
    except Exception as exc:  # noqa: BLE001 - soft-fail by design
        print(
            f"prep synthesis unavailable: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 0

    def run(pair: tuple[dict[str, Any], dict[str, Any]]) -> bool:
        item, evidence = pair
        result = synthesize_one(str(item.get("id") or ""), evidence, client, model_id)
        if result is None:
            return False
        apply_synthesis(item, result)
        return True

    workers = min(MAX_WORKERS, len(pending))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return sum(pool.map(run, pending))
