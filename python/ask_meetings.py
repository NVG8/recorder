"""Answer questions across the whole meeting corpus, with citations.

Each question sees two things: an INDEX of every decision and action item across
the *whole* archive, and FULL DETAIL (attendees, summary, transcript) for the
meetings most likely to answer it — ranked by BM25, plus any meeting whose date
the question names. `MAX_MEETINGS=0` sends full detail for everything.

The split follows where the bulk is. Transcripts are most of the tokens and are
what needs narrowing; the structured layer is small and is what aggregate
questions need in full. Filtering alone breaks "what am I still waiting on",
because that question has no distinctive terms to match and its answer is
spread everywhere.

Why filter at all, given it all fits: **accuracy degrades before capacity does.**
Across a large archive of structurally similar meetings, a broad question
("what was decided, and what did I commit to?") can come back citing the right
meeting with another meeting's content. The same question over the top 40
answers correctly.

Sizing: Bedrock's *default* window for this model is 200K tokens; the
long-context beta (`LONG_CONTEXT_BETA`) lifts it to 1M.

Filtering trades away the prompt cache, since the prefix now varies per
question. That is usually cheaper anyway: most questions are one-offs, and an
uncached filtered question costs far less than warming a full-archive cache.

Which is why citations are verified, not trusted. Both halves matter: the
recording must exist, *and* the quote must actually appear in it. Checking only
the id is not enough — observed in practice, the model cited a real meeting with
the right date and title while quoting entirely different meetings, which is more
dangerous than an invented id because nothing about it looks wrong. Citations
that fail either check are dropped, and an answer that loses all of them is
forced to low confidence.

Usage:
  uv run python ask_meetings.py "what did we decide about the pricing change?"
  uv run python ask_meetings.py --stats
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import recorder_config  # noqa: F401  (loads ~/.config/recorder/.env)

RECORDINGS = Path(
    os.getenv(
        "RECORDER_RECORDINGS_DIR",
        str(Path.home() / "Library/Application Support/Recorder/recordings"),
    )
)

DEFAULT_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
DEFAULT_MODEL = os.environ.get(
    "BEDROCK_ASK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)

# Leave headroom under the model's context limit. Oldest meetings are dropped
# first when the corpus outgrows this — recent context is what gets asked about.
MAX_CORPUS_CHARS = int(os.getenv("RECORDER_ASK_MAX_CHARS", "3200000"))

# How many meetings a single question is allowed to see. Capacity is not the
# binding constraint — accuracy is; see the module docstring. 0 disables the
# pre-filter and sends everything.
MAX_MEETINGS = int(os.getenv("RECORDER_ASK_MAX_MEETINGS", "40"))

# Lifts this model's Bedrock context window from 200K to 1M.
LONG_CONTEXT_BETA = os.getenv("BEDROCK_LONG_CONTEXT_BETA", "context-1m-2025-08-07")

_STEM_RE = re.compile(r"^rec-(\d{8})-(\d{6})")

SYSTEM_PROMPT = """\
You answer questions about the user's meeting history. You are given two sections:

- INDEX: every decision and action item from every meeting on file, one line \
each, tagged with owner and bucket (`mine` = the user owes it, `waiting_on` = the \
user is owed it, `fyi` = neither). Use this for questions that span the archive — \
what is outstanding, who owes what, how something changed over time.
- FULL DETAIL: the meetings most relevant to this question, with attendees, \
summary and full transcript. Use this for depth on a specific meeting.

Cite from either. A meeting in the INDEX but not in FULL DETAIL is still a \
legitimate citation — quote its decision or action-item line verbatim.

How to answer:
- Answer only from the meetings provided. If they don't contain the answer, say \
so plainly — "no meeting on file covers that" is a correct and useful answer. \
Never fill a gap with plausible-sounding inference.
- Lead with the answer in the first sentence. Add supporting detail after.
- Synthesize across meetings when the question spans several. Note when \
something changed over time ("agreed X in June, revisited in July").
- Prefer specifics — names, numbers, dates, commitments — over paraphrase.
- Distinguish what was *decided* from what was merely *discussed*, and who owns \
an action item from who raised it.

Citations are mandatory and are how the reader checks you. Cite every meeting \
you drew on:
- `recordingId` must be copied exactly from the `[rec-...]` marker of the \
meeting you used. Never construct, guess, or adjust one.
- `quote` must be text that appears verbatim in that meeting's content. Keep it \
short — one sentence is plenty.
- If you cannot produce a real quote for a claim, leave the claim out.
"""

# The output contract lives in the user turn, not the system prompt. With a
# large archive between the two, an instruction at the top of the system
# prompt is too far from the point of generation to be followed reliably — the
# model answers in prose and ignores the schema. Restating it adjacent to the
# question fixes that, and it keeps the volatile part of the request outside the
# cached prefix.
USER_TEMPLATE = """\
Today is {today}.
{date_hint}
Question: {question}

Answer using only the meeting archive above.

Return JSON only — no prose outside it, no code fences:

{{
  "answer": "Your answer in plain prose. Markdown is fine for lists.",
  "citations": [
    {{"recordingId": "rec-...", "quote": "verbatim text from that meeting"}}
  ],
  "confidence": "high" | "medium" | "low"
}}

Copy each `recordingId` exactly from the `[rec-...]` marker of the meeting you \
used.

Each `quote` must be **copied character-for-character** from that meeting's \
text — not summarized, reworded, tidied, or stitched together from two places. \
Quotes are checked against the source and a paraphrase is discarded, so \
rewording one costs you the citation entirely. Copy a short exact run, or omit \
the citation.

When you are citing a meeting for its *existence* rather than for something \
said in it — listing which meetings happened, for instance — set `quote` to an \
empty string. That is expected and costs nothing. Never invent or approximate a \
quote to fill the field.

If the honest answer is that nothing was decided or nothing matches, say so and \
return no citations — that is a good answer, not a failed one.

Use "low" confidence when the archive only partly covers the question, and say \
what is missing in the answer itself.
"""


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _meeting_datetime(stem: str) -> datetime | None:
    match = _STEM_RE.match(stem)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def load_meetings() -> list[dict[str, Any]]:
    """Gather every recording into (newest-first) structured records."""
    meetings: list[dict[str, Any]] = []
    for transcript_path in RECORDINGS.glob("*.transcript.json"):
        stem = transcript_path.name.replace(".transcript.json", "")
        transcript = _read_json(transcript_path).get("text") or ""
        if not transcript.strip():
            continue
        event = _read_json(transcript_path.with_name(f"{stem}.event.json"))
        todos = _read_json(transcript_path.with_name(f"{stem}.todos.json"))
        when = _meeting_datetime(stem)
        meetings.append(
            {
                "id": stem,
                "date": when.strftime("%Y-%m-%d") if when else "",
                "sortKey": when or datetime.min,
                "title": (event.get("title") or "").strip() or "(untitled meeting)",
                "attendees": [
                    a for a in (event.get("attendees") or []) if isinstance(a, dict)
                ],
                "summary": (todos.get("summary") or "").strip(),
                "decisions": todos.get("decisions") or [],
                "todos": todos.get("todos") or [],
                "transcript": transcript.strip(),
            }
        )
    meetings.sort(key=lambda m: m["sortKey"], reverse=True)
    return meetings


def render_meeting(meeting: dict[str, Any]) -> str:
    lines = [f"### [{meeting['id']}] {meeting['date']} — {meeting['title']}"]

    who = []
    for att in meeting["attendees"]:
        name = (att.get("name") or "").strip()
        email = (att.get("email") or "").strip()
        who.append(f"{name} <{email}>" if name and email else (name or email))
    if who:
        lines.append("Attendees: " + ", ".join(w for w in who if w))

    if meeting["summary"]:
        lines.append(f"Summary: {meeting['summary']}")

    if meeting["decisions"]:
        lines.append("Decisions:")
        for d in meeting["decisions"]:
            text = (d.get("text") or "").strip()
            if text:
                lines.append(f"- {text}")

    if meeting["todos"]:
        lines.append("Action items:")
        for t in meeting["todos"]:
            text = (t.get("text") or "").strip()
            if not text:
                continue
            owner = (t.get("owner") or "").strip() or "unassigned"
            due = (t.get("due_iso") or t.get("due") or "").strip()
            suffix = f" (due {due})" if due else ""
            lines.append(f"- [{owner}] {text}{suffix}")

    lines.append("Transcript:")
    lines.append(meeting["transcript"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Relevance pre-filter
#
# Sending every meeting can fit the context window but degrades the answer:
# structurally similar meetings blend together, and a broad question comes back
# citing the right meeting with another meeting's content. Narrowing to the
# meetings a question is actually about fixes that.
#
# BM25 over the raw text, no embeddings. The corpus is small, the vocabulary is
# the user's own (company names, people, products), and lexical matching is
# debuggable in a way a vector score is not — when a meeting is wrongly included
# or missed you can see exactly which term did it.
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "a", "about", "after", "all", "am", "an", "and", "any", "are", "around", "as",
    "at", "be", "been", "before", "being", "but", "by", "can", "did", "do", "does",
    "for", "from", "get", "had", "has", "have", "how", "i", "if", "in", "is", "it",
    "its", "just", "me", "my", "of", "on", "or", "our", "out", "so", "that", "the",
    "their", "them", "then", "there", "these", "they", "this", "to", "up", "was",
    "we", "were", "what", "when", "where", "which", "who", "why", "will", "with",
    "would", "you", "your",
}

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return [
        t for t in _TOKEN_RE.findall((text or "").lower())
        if t not in _STOPWORDS and len(t) > 1
    ]


def dates_in_question(question: str) -> set[str]:
    """Explicit dates named in the question, as YYYY-MM-DD.

    A question that names a date is asking about *that* meeting, and lexical
    similarity is a poor way to honour it — "August 5" shares no distinctive
    terms with the meeting's content. Those meetings are force-included instead
    of ranked.
    """
    found: set[str] = set()
    q = question.lower()
    for match in re.finditer(r"(\d{4})-(\d{2})-(\d{2})", q):
        found.add(match.group(0))
    # "August 5", "August 5 2026", "5 August 2026"
    for name, month in _MONTHS.items():
        for match in re.finditer(
            rf"\b(?:{name}\s+(\d{{1,2}})|(\d{{1,2}})\s+{name})\b(?:,?\s*(\d{{4}}))?", q
        ):
            day = match.group(1) or match.group(2)
            year = match.group(3) or str(datetime.now().year)
            try:
                found.add(datetime(int(year), month, int(day)).strftime("%Y-%m-%d"))
            except ValueError:
                continue
    # Relative ranges. Meetings are labelled by date, so the useful move is to
    # force-include every meeting in the range rather than hope BM25 finds them
    # — "this week" shares no vocabulary with the meetings it refers to.
    today = datetime.now()
    if re.search(r"\bthis week\b", q):
        start = today - timedelta(days=today.weekday())
        found.update((start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7))
    if re.search(r"\blast week\b", q):
        start = today - timedelta(days=today.weekday() + 7)
        found.update((start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7))
    if re.search(r"\bthis month\b", q):
        first = today.replace(day=1)
        found.update(
            (first + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(31)
            if (first + timedelta(days=i)).month == today.month
        )
    if re.search(r"\btoday\b", q):
        found.add(today.strftime("%Y-%m-%d"))
    if re.search(r"\byesterday\b", q):
        found.add((today - timedelta(days=1)).strftime("%Y-%m-%d"))
    return found


def date_hint(question: str, meetings: list[dict[str, Any]]) -> str:
    """Resolve relative dates in the question and name the meetings they hit.

    The model handles the arithmetic fine — asked "yesterday" it names the right
    date — but then can fail to *find* that date among hundreds of archive
    entries and conclude no such meeting exists. The same question with
    an explicit date works, because the date is then a literal string to scan
    for.

    So the lookup is done here, deterministically, and the answer is handed over
    rather than searched for. Empty when the question names no dates, which
    leaves ordinary questions untouched.
    """
    wanted = dates_in_question(question)
    if not wanted:
        return ""
    hits = [m for m in meetings if m["date"] in wanted]
    span = f"{min(wanted)}" if len(wanted) == 1 else f"{min(wanted)} to {max(wanted)}"
    if not hits:
        return (
            f"\nThe question refers to {span}. There are no meetings on file for "
            f"that date range — say so plainly.\n"
        )
    lines = "\n".join(
        f"  [{m['id']}] {m['date']} - {m['title']}" for m in sorted(hits, key=lambda x: x["date"])
    )
    return (
        f"\nThe question refers to {span}. The meetings on file for that range "
        f"are exactly:\n{lines}\nUse these; do not conclude the date is missing "
        f"from the archive.\n"
    )


def rank_meetings(
    question: str, meetings: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    """Meetings most likely to answer `question`, newest-first within the cut.

    Returns everything when the archive is already small enough — filtering a
    handful of meetings only risks dropping the answer.
    """
    if limit <= 0 or len(meetings) <= limit:
        return meetings

    docs = [tokenize(_searchable_raw(m)) for m in meetings]
    lengths = [len(d) or 1 for d in docs]
    avg_len = sum(lengths) / len(lengths)

    freq: list[dict[str, int]] = []
    doc_count: dict[str, int] = {}
    for doc in docs:
        counts: dict[str, int] = {}
        for term in doc:
            counts[term] = counts.get(term, 0) + 1
        freq.append(counts)
        for term in counts:
            doc_count[term] = doc_count.get(term, 0) + 1

    terms = tokenize(question)
    n = len(meetings)
    k1, b = 1.5, 0.75

    scored: list[tuple[float, int]] = []
    for i, counts in enumerate(freq):
        score = 0.0
        for term in terms:
            tf = counts.get(term, 0)
            if not tf:
                continue
            df = doc_count.get(term, 0) or 1
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            norm = tf * (k1 + 1) / (tf + k1 * (1 - b + b * lengths[i] / avg_len))
            score += idf * norm
        scored.append((score, i))

    wanted = dates_in_question(question)
    forced = {i for i, m in enumerate(meetings) if m["date"] in wanted}

    scored.sort(key=lambda x: x[0], reverse=True)
    chosen = list(forced)
    for score, i in scored:
        if len(chosen) >= limit:
            break
        if i not in forced and score > 0:
            chosen.append(i)

    # A question with no lexical overlap at all (or only a date) still deserves
    # context; fall back to the most recent meetings rather than sending none.
    if len(chosen) < min(limit, len(meetings)):
        for i in range(len(meetings)):
            if len(chosen) >= limit:
                break
            if i not in chosen:
                chosen.append(i)

    chosen_set = set(chosen)
    return [m for i, m in enumerate(meetings) if i in chosen_set]


def _searchable_raw(meeting: dict[str, Any]) -> str:
    parts = [meeting["title"], meeting["summary"], meeting["transcript"]]
    parts += [str(d.get("text") or "") for d in meeting["decisions"]]
    parts += [
        f"{t.get('owner') or ''} {t.get('text') or ''}" for t in meeting["todos"]
    ]
    for att in meeting["attendees"]:
        parts.append(f"{att.get('name') or ''} {att.get('email') or ''}")
    return " ".join(parts)


def index_lines(meeting: dict[str, Any]) -> list[str]:
    """The decision/action lines shown for one meeting in the INDEX.

    Shared with citation verification on purpose: the model can only be held to
    quoting what it was actually shown, so the text it sees and the text we
    check against must be produced by the same code. When these drifted apart,
    a verbatim index quote failed verification because the searchable form
    omitted the `[bucket]` prefix.
    """
    entries: list[str] = []
    for decision in meeting["decisions"]:
        text = str(decision.get("text") or "").strip()
        if text:
            entries.append(f"DECIDED: {text}")
    for todo in meeting["todos"]:
        text = str(todo.get("text") or "").strip()
        if not text:
            continue
        owner = str(todo.get("owner") or "").strip() or "unassigned"
        bucket = str(todo.get("bucket") or "fyi").strip()
        due = str(todo.get("due_iso") or todo.get("due") or "").strip()
        suffix = f" (due {due})" if due else ""
        entries.append(f"[{bucket}] {owner}: {text}{suffix}")
    return entries


def build_index(meetings: list[dict[str, Any]]) -> str:
    """One compact line per decision and action item, for *every* meeting.

    Retrieval answers "what happened in X" but structurally cannot answer
    "what am I still waiting on" — that question has no distinctive terms to
    match, and its answer is spread across the whole archive. The top 40
    meetings by relevance hold only a fraction of the outstanding items.

    So the structured layer ships whole and only the transcripts get filtered.
    That split works because it is where the bulk actually is: every decision
    and todo is a small fraction of the transcript tokens.
    """
    lines: list[str] = []
    for meeting in meetings:
        entries = index_lines(meeting)
        if entries:
            lines.append(f"[{meeting['id']}] {meeting['date']} - {meeting['title']}")
            lines.extend(f"  {e}" for e in entries)
    return "\n".join(lines)


def build_corpus(meetings: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Render meetings newest-first until the character budget is spent."""
    blocks: list[str] = []
    included: list[dict[str, Any]] = []
    total = 0
    for meeting in meetings:
        block = render_meeting(meeting)
        if total + len(block) > MAX_CORPUS_CHARS and included:
            break
        blocks.append(block)
        included.append(meeting)
        total += len(block)
    return "\n\n".join(blocks), included


# ---------------------------------------------------------------------------
# Bedrock
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    return json.loads(text)


def _converse(
    client: Any, model_id: str, corpus: str, index: str, question: str, hint: str = ""
) -> str:  # noqa: C901
    """Send the corpus with a cache point so follow-up questions are cheap.

    Bedrock rejects `cachePoint` on models or regions that don't support it, so
    fall back to an uncached call rather than failing the question outright.
    """
    system_cached = [
        {"text": SYSTEM_PROMPT},
        {
            "text": (
                "=== INDEX: decisions and action items from EVERY meeting ===\n\n"
                f"{index}\n\n"
                "=== FULL DETAIL: the meetings most relevant to this question ===\n\n"
                f"{corpus}"
            )
        },
        {"cachePoint": {"type": "default"}},
    ]
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "text": USER_TEMPLATE.format(
                        question=question,
                        today=datetime.now().astimezone().strftime("%A, %d %B %Y"),
                        date_hint=hint,
                    )
                }
            ],
        }
    ]
    inference = {"maxTokens": 4096, "temperature": 0.0}
    # Bedrock defaults this model to a 200K window, which a large archive
    # passes. The long-context beta lifts it to 1M, which is what keeps the
    # send-everything design viable — without it this needs real retrieval.
    extra = {"anthropic_beta": [LONG_CONTEXT_BETA]}

    def call(system: list[dict], fields: dict | None) -> dict:
        kwargs: dict[str, Any] = dict(
            modelId=model_id, system=system, messages=messages, inferenceConfig=inference
        )
        if fields:
            kwargs["additionalModelRequestFields"] = fields
        return client.converse(**kwargs)

    try:
        resp = call(system_cached, extra)
    except Exception as exc:  # noqa: BLE001 - degrade, don't fail the question
        text = str(exc)
        if "prompt is too long" in text:
            # Even 1M has a ceiling. Fail loudly rather than quietly answering
            # from a truncated archive the caller never agreed to.
            raise RuntimeError(
                f"meeting archive exceeds the model context window ({text}). "
                "Lower RECORDER_ASK_MAX_CHARS to search fewer (newer) meetings, "
                "or add a retrieval step."
            ) from exc
        if "cachePoint" not in text and "validation" not in text.lower():
            raise
        print(f"prompt caching unavailable ({exc}); retrying uncached", file=sys.stderr)
        resp = call([s for s in system_cached if "cachePoint" not in s], extra)

    usage = resp.get("usage") or {}
    cached = usage.get("cacheReadInputTokens") or 0
    print(
        f"tokens: in={usage.get('inputTokens')} cached_read={cached} "
        f"out={usage.get('outputTokens')}",
        file=sys.stderr,
    )
    parts = resp["output"]["message"]["content"]
    return "".join(p.get("text", "") for p in parts).strip()


_PUNCT = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WS.sub(" ", _PUNCT.sub(" ", (text or "").lower())).strip()


def _searchable(meeting: dict[str, Any]) -> str:
    """Everything the model was shown for one meeting, flattened for matching.

    Must cover *every* surface the model can read, header included. A listing
    question ("what meetings did I have this week?") naturally cites a meeting
    by quoting its title — and while the title is right there in the header the
    model sees, leaving it out here silently discarded those citations.
    """
    parts = [meeting["title"], meeting["date"], meeting["transcript"], meeting["summary"]]
    parts += [
        f"{a.get('name') or ''} {a.get('email') or ''}" for a in meeting["attendees"]
    ]
    parts += index_lines(meeting)
    return _normalize(" ".join(parts))


def verify_citations(
    raw: list[Any], meetings: list[dict[str, Any]]
) -> tuple[list[dict[str, str]], list[str]]:
    """Keep only citations naming a real meeting *and* quoting it.

    Checking the id alone is not enough. Observed in practice: the model cited a
    real recording — correct date, correct title, clickable — while the quotes
    came from entirely different meetings. That is more dangerous than an
    invented id, because everything about it looks right. So the quote must
    actually appear in the meeting it is attributed to.

    Matching is whitespace- and punctuation-insensitive, and accepts a long
    prefix, so trivial reformatting doesn't cost a real citation. Anything
    looser would defeat the point.
    """
    by_id = {m["id"]: m for m in meetings}
    haystacks: dict[str, str] = {}
    kept: list[dict[str, str]] = []
    dropped: list[str] = []

    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        rec_id = str(entry.get("recordingId") or "").strip()
        meeting = by_id.get(rec_id)
        if meeting is None:
            dropped.append(f"{rec_id or '(missing id)'}: no such recording")
            continue

        quote = str(entry.get("quote") or "").strip()
        # An empty quote is allowed. Some questions — "what meetings did I have
        # this week?" — cite a meeting for its existence, not for a claim made
        # inside it, and there is no atomic fact to quote. Forcing a quote there
        # just pushed the model into paraphrasing, which then failed
        # verification and sank a correct answer to low confidence.
        #
        # What stays strict is the dangerous case: a quote that is *offered* and
        # does not check out. Silence is honest; a confident misquote is not.
        if quote:
            if rec_id not in haystacks:
                haystacks[rec_id] = _searchable(meeting)
            blob = haystacks[rec_id]
            needle = _normalize(quote)
            # A short needle would match almost anything; require real substance.
            probe = needle[:80] if len(needle) > 80 else needle
            if len(probe) < 16 or probe not in blob:
                dropped.append(f"{rec_id}: quote not found in that meeting")
                continue

        kept.append(
            {
                "recordingId": rec_id,
                "date": meeting["date"],
                "title": meeting["title"],
                "quote": quote,
            }
        )
    return kept, dropped


def ask(
    question: str,
    region: str = DEFAULT_REGION,
    model_id: str = DEFAULT_MODEL,
    max_meetings: int = MAX_MEETINGS,
) -> dict[str, Any]:
    meetings = load_meetings()
    if not meetings:
        return {"answer": "No meetings on file yet.", "citations": [], "confidence": "low"}

    candidates = rank_meetings(question, meetings, max_meetings)
    corpus, included = build_corpus(candidates)
    index = build_index(meetings)

    import boto3

    client = boto3.client("bedrock-runtime", region_name=region)
    parsed = _extract_json(
        _converse(client, model_id, corpus, index, question, date_hint(question, meetings))
    )

    # Verify against the whole archive: the index exposes meetings that were
    # not retrieved in full, and citing one of those is legitimate.
    citations, dropped = verify_citations(parsed.get("citations"), meetings)
    if dropped:
        print(f"dropped {len(dropped)} unverifiable citation(s): {dropped}", file=sys.stderr)

    confidence = parsed.get("confidence")
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"
    # An answer whose every citation failed verification is an answer we cannot
    # stand behind, whatever the model claimed about its own certainty.
    if dropped and not citations:
        confidence = "low"

    return {
        "question": question,
        "answer": (parsed.get("answer") or "").strip(),
        "citations": citations,
        "confidence": confidence,
        "meetingsSearched": len(included),
        "meetingsAvailable": len(meetings),
        "droppedCitations": len(dropped),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="?", default="", help="Question to answer")
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--stats", action="store_true", help="Print corpus size and exit")
    args = parser.parse_args()

    if args.stats:
        meetings = load_meetings()
        corpus, included = build_corpus(meetings)
        json.dump(
            {
                "meetings": len(meetings),
                "included": len(included),
                "chars": len(corpus),
                "approxTokens": len(corpus) // 4,
                "decisions": sum(len(m["decisions"]) for m in meetings),
                "todos": sum(len(m["todos"]) for m in meetings),
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0

    if not args.question.strip():
        print("a question is required", file=sys.stderr)
        return 2

    try:
        result = ask(args.question, region=args.region, model_id=args.model)
    except Exception as exc:  # noqa: BLE001 - surface as JSON for the app
        json.dump({"error": f"{type(exc).__name__}: {exc}"}, sys.stdout)
        sys.stdout.write("\n")
        return 1

    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
