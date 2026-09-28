#!/usr/bin/env python3
"""Gmail-API-backed drop-in replacement for NotmuchSearch.

Same ``search_emails()`` signature and return-dict shape as
``notmuch_search.NotmuchSearch`` so it can run side-by-side with (and
eventually replace) the notmuch backend.

Return dict per message:
    message_id, rfc822_message_id, subject, sender, recipient, date,
    tags, account, body_text (if include_body), similarity_score

``message_id`` is Gmail's opaque id (used for dedup by callers).
``rfc822_message_id`` is the RFC822 Message-Id header — this is what
notmuch reports as its ``message_id``, so it's the key to compare the two
backends on.
"""

import base64
import html as _html
import logging
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import recorder_config as rc

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Account short-name -> email addresses, from recorder_config.
ACCOUNT_EMAILS: Dict[str, List[str]] = {name: rc.account_emails(name) for name in rc.ACCOUNTS}

# Resolving relative to this file would point inside the app bundle, which has
# no OAuth tokens, so read them from the recorder data root.
_CONFIG_DIR = rc.CONFIG_DIR


class GmailSearch:
    """Gmail-API email search with a notmuch-compatible interface."""

    # Gmail caps a batch at 100, but its per-user *concurrency* limit is far
    # lower and batch sub-requests run concurrently — keep batches small.
    _BATCH_SIZE = 20
    _MAX_RETRIES = 5
    _BACKOFF_BASE = 1.0  # seconds; doubles each retry, plus jitter.

    def __init__(
        self,
        account: str = rc.DEFAULT_ACCOUNT,
        credentials_path: Optional[str] = None,
        token_path: Optional[str] = None,
    ):
        self.account = account.lower()
        self.credentials_path = Path(
            credentials_path or rc.CLIENT_SECRETS
        )
        # Per-account Gmail token, kept separate from the calendar/drive tokens
        # because Gmail needs its own consent (scope can't be silently appended).
        self.token_path = Path(
            token_path or (_CONFIG_DIR / f"gmail_token_{self.account}.json")
        )
        self._service = None

    # ── auth ─────────────────────────────────────────────────────────
    def _get_service(self):
        if self._service is not None:
            return self._service

        creds: Optional[Credentials] = None
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not self.credentials_path.exists():
                    raise FileNotFoundError(
                        f"OAuth client not found at {self.credentials_path}"
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.credentials_path), SCOPES
                )
                # First run: opens a browser. Log in as the *account* mailbox
                # (e.g. you@client.com), not the primary login.
                creds = flow.run_local_server(port=0)
            rc.write_secret(self.token_path, creds.to_json())

        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    # ── query translation ────────────────────────────────────────────
    @staticmethod
    def _translate_query(query: str) -> str:
        """notmuch query string -> Gmail q= fragment."""
        if not query or query.strip() in ("*", ""):
            return ""
        q = query
        # Gmail has no field-scoped body: operator — a bare (quoted) phrase
        # searches the whole message. body:"x" -> "x", body:x -> x.
        q = re.sub(r'\bbody:"([^"]*)"', r'"\1"', q)
        q = re.sub(r"\bbody:(\S+)", r"\1", q)
        # notmuch AND -> Gmail implicit AND (space). OR is the same in both.
        q = re.sub(r"\bAND\b", " ", q)
        # Collapse whitespace.
        q = re.sub(r"\s+", " ", q).strip()
        return q

    @staticmethod
    def _to_gmail_date(date_str: str) -> str:
        """YYYY-MM-DD -> YYYY/MM/DD (Gmail after:/before: format)."""
        return date_str.replace("-", "/")

    def _date_terms(
        self,
        date_filter: Optional[str],
        date_from: Optional[str],
        date_to: Optional[str],
    ) -> List[str]:
        terms: List[str] = []
        if date_filter:
            df = date_filter.strip().lower()
            # notmuch's relative dates snap to a calendar-day (midnight)
            # boundary; Gmail's newer_than:Nd is a rolling 24h cutoff, which
            # drops messages from the boundary day. Translate day/week filters
            # to an explicit after:<calendar date> to match notmuch.
            m = re.match(r"^(\d+)([dw])$", df)
            if m:
                days = int(m.group(1)) * (7 if m.group(2) == "w" else 1)
                since = datetime.now() - timedelta(days=days)
                return [f"after:{since.strftime('%Y/%m/%d')}"]
            m = re.match(r"^(\d+)m$", df)
            if m:
                return [f"newer_than:{m.group(1)}m"]
            m = re.match(r"^(\d+)y$", df)
            if m:
                return [f"newer_than:{m.group(1)}y"]
            m = re.match(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$", df)
            if m:
                start, end = m.group(1), m.group(2)
                terms.append(f"after:{self._to_gmail_date(start)}")
                terms.append(f"before:{self._end_exclusive(end)}")
                return terms
            m = re.match(r"^(\d{4}-\d{2}-\d{2})$", df)
            if m:
                day = m.group(1)
                terms.append(f"after:{self._to_gmail_date(day)}")
                terms.append(f"before:{self._end_exclusive(day)}")
                return terms
        if date_from:
            terms.append(f"after:{self._to_gmail_date(date_from)}")
        if date_to:
            terms.append(f"before:{self._end_exclusive(date_to)}")
        return terms

    @staticmethod
    def _end_exclusive(date_str: str) -> str:
        """Gmail before: is exclusive; notmuch ranges are inclusive of the
        end day. Bump end date by one day so the last day is included."""
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
            return d.strftime("%Y/%m/%d")
        except ValueError:
            return date_str.replace("-", "/")

    def _build_query(
        self,
        query: str = "*",
        account: Optional[str] = None,
        date_filter: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        tags: Optional[List[str]] = None,
        sender: Optional[str] = None,
        recipient: Optional[str] = None,
        subject: Optional[str] = None,
    ) -> str:
        parts: List[str] = []

        translated = self._translate_query(query)
        if translated:
            parts.append(f"({translated})" if " OR " in translated else translated)

        acct = (account or self.account or "").lower()
        emails = ACCOUNT_EMAILS.get(acct, [])
        if emails:
            clauses = []
            for email in emails:
                # notmuch's `to:` matches To/Cc/Bcc; Gmail's `to:` is To-only,
                # so expand to cc:/bcc: to keep cc-only mail (parity fix).
                clauses.append(f"from:{email}")
                clauses.append(f"to:{email}")
                clauses.append(f"cc:{email}")
                clauses.append(f"bcc:{email}")
            parts.append("(" + " OR ".join(clauses) + ")")

        parts.extend(self._date_terms(date_filter, date_from, date_to))

        if tags:  # notmuch tag: -> Gmail label:
            for tag in tags:
                parts.append(f"label:{tag}")
        if sender:
            parts.append(f"from:{sender}")
        if recipient:
            parts.append(f"to:{recipient}")
        if subject:
            parts.append(f'subject:"{subject}"')

        return " ".join(parts).strip()

    # ── search ────────────────────────────────────────────────────────
    def search_emails(
        self,
        query: str = "*",
        limit: int = 10,
        account: Optional[str] = None,
        date_filter: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        tags: Optional[List[str]] = None,
        sender: Optional[str] = None,
        recipient: Optional[str] = None,
        subject: Optional[str] = None,
        include_body: bool = False,
    ) -> List[Dict]:
        service = self._get_service()
        q = self._build_query(
            query=query,
            account=account,
            date_filter=date_filter,
            date_from=date_from,
            date_to=date_to,
            tags=tags,
            sender=sender,
            recipient=recipient,
            subject=subject,
        )
        logger.info("Gmail query: %s", q)

        ids = self._list_message_ids(service, q, limit)
        if not ids:
            return []
        return self._fetch_messages(service, ids, include_body)

    def _fetch_messages(
        self, service, ids: List[str], include_body: bool
    ) -> List[Dict]:
        """Fetch messages in batches. Gmail requires one get() per message;
        batching cuts round-trips vs a serial loop. Batch sub-requests run
        concurrently and Gmail's per-user concurrency limit is low, so we keep
        batches small and retry 429/5xx sub-requests with exponential backoff.
        Callbacks fire out of order, so results are keyed by index and
        reassembled to preserve messages.list' most-recent-first order."""
        # metadata is enough (and cheaper) when we don't need the body.
        fmt = "full" if include_body else "metadata"
        by_index: Dict[int, Dict] = {}
        pending = list(enumerate(ids))  # [(index, msg_id), ...]

        for attempt in range(self._MAX_RETRIES + 1):
            retry: List[tuple] = []

            def _callback(request_id, response, exception, _retry=retry):
                idx = int(request_id)
                if exception is None:
                    by_index[idx] = self._to_email_dict(response, include_body)
                    return
                status = getattr(getattr(exception, "resp", None), "status", None)
                if status in (429, 500, 503):
                    _retry.append((idx, ids[idx]))  # transient — try again
                else:
                    logger.warning("Gmail get failed (%s): %s", request_id, exception)

            for start in range(0, len(pending), self._BATCH_SIZE):
                batch = service.new_batch_http_request(callback=_callback)
                for idx, msg_id in pending[start : start + self._BATCH_SIZE]:
                    req = (
                        service.users()
                        .messages()
                        .get(
                            userId="me",
                            id=msg_id,
                            format=fmt,
                            metadataHeaders=[
                                "From", "To", "Subject", "Date", "Message-Id",
                            ],
                        )
                    )
                    batch.add(req, request_id=str(idx))
                batch.execute()

            if not retry:
                break
            pending = retry
            if attempt < self._MAX_RETRIES:
                sleep = self._BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.5)
                logger.info("Gmail throttled %d msg(s); backing off %.1fs",
                            len(retry), sleep)
                time.sleep(sleep)
        else:
            logger.warning("Gmail: gave up on %d message(s) after retries", len(pending))

        return [by_index[i] for i in sorted(by_index)]

    def _list_message_ids(self, service, q: str, limit: int) -> List[str]:
        ids: List[str] = []
        page_token = None
        while len(ids) < limit:
            resp = (
                service.users()
                .messages()
                .list(
                    userId="me",
                    q=q,
                    maxResults=min(500, limit - len(ids)),
                    pageToken=page_token,
                    includeSpamTrash=False,
                )
                .execute()
            )
            ids.extend(m["id"] for m in resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return ids[:limit]

    # ── parsing ───────────────────────────────────────────────────────
    def _to_email_dict(self, msg: Dict[str, Any], include_body: bool) -> Dict[str, Any]:
        payload = msg.get("payload", {})
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in payload.get("headers", [])
        }
        rfc822 = headers.get("message-id", "").strip().strip("<>")
        label_ids = msg.get("labelIds", []) or []
        tags = [l.lower() for l in label_ids]
        # notmuch marks outbound mail with the 'sent' tag; Gmail uses the SENT
        # label. Normalize so `'sent' in tags` keeps working downstream.
        if "sent" not in tags and "SENT" in label_ids:
            tags.append("sent")

        email: Dict[str, Any] = {
            "message_id": msg.get("id", ""),
            "rfc822_message_id": rfc822,
            "thread_id": msg.get("threadId", ""),
            "subject": headers.get("subject", ""),
            "sender": headers.get("from", ""),
            "recipient": headers.get("to", ""),
            "date": headers.get("date", ""),
            "tags": tags,
            "account": self.account,
            "similarity_score": 1.0,
        }
        if include_body:
            email["body_text"] = self._extract_body(payload)
        return email

    def _extract_body(self, payload: Dict[str, Any]) -> str:
        plain = self._find_part(payload, "text/plain")
        if plain:
            return plain
        html_body = self._find_part(payload, "text/html")
        if html_body:
            return self._html_to_text(html_body)
        return ""

    def _find_part(self, part: Dict[str, Any], mime_type: str) -> str:
        if part.get("mimeType") == mime_type:
            data = part.get("body", {}).get("data")
            if data:
                return self._decode(data)
        for sub in part.get("parts", []) or []:
            found = self._find_part(sub, mime_type)
            if found:
                return found
        return ""

    @staticmethod
    def _decode(data: str) -> str:
        try:
            return base64.urlsafe_b64decode(data.encode("utf-8")).decode(
                "utf-8", errors="replace"
            )
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _html_to_text(html_body: str) -> str:
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html_body)
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</p>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = _html.unescape(text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
        return text.strip()
