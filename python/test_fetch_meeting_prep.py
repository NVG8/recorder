from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import fetch_meeting_prep as subject


class BriefParserTests(unittest.TestCase):
    def test_parse_saved_brief_contract(self) -> None:
        markdown = """# Acme <> Example Co - Weekly Huddle

- **When**: Thursday 2026-05-28 09:00 PDT-09:30 (30 min)
- **Location**: Microsoft Teams Meeting
- **Calendar**: [Open in Google Calendar](https://calendar.example/event)

## Attendees (external)
- **Jane Doe** — `jane@acme.example` — @ Acme
- **John Roe** — `john@acme.example` — @ Globex.example

## Why this meeting matters

This is the reason.

## Background

- **Closed Won** partnership deal.
- Last huddle was **canceled 5/20**.

## Talking points

- **Integration details** — Bring specifics.

## Open questions

- What is the ETA?

## Suggested ask

- **Get a committed ETA** from product.
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "0900_test.md"
            path.write_text(markdown, encoding="utf-8")

            brief = subject.parse_brief(path)

        self.assertEqual(brief.location, "Microsoft Teams Meeting")
        self.assertEqual(brief.calendar_link, "https://calendar.example/event")
        self.assertEqual(
            brief.attendees[0],
            {"name": "Jane Doe", "email": "jane@acme.example", "company": "Acme"},
        )
        self.assertEqual(brief.why, "This is the reason.")
        self.assertEqual(brief.background, ["Closed Won partnership deal.", "Last huddle was canceled 5/20."])
        self.assertEqual(brief.talking_points, ["Integration details — Bring specifics."])
        self.assertEqual(brief.open_questions, ["What is the ETA?"])
        self.assertEqual(brief.suggested_asks, ["Get a committed ETA from product."])


if __name__ == "__main__":
    unittest.main()
