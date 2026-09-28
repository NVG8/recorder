"""Email-search backend selection for the Recorder sidecar.

Lets each account read mail from whichever backend actually holds it: a local
notmuch index, or the Gmail API. If another tool on the machine (say, a daily
brief generator) reads the same mailbox, point both at the same backend so they
never show different email for the same meeting.

Backend is chosen by env vars, which `fetch_meeting_prep.py` also loads from
`<data_root>/.env`:

    EMAIL_SEARCH_BACKEND            global default: "notmuch" (default) | "gmail"
    EMAIL_SEARCH_BACKEND_<ACCOUNT>  per-account override, e.g.
                                    EMAIL_SEARCH_BACKEND_WORK=gmail

Gmail auth is per-mailbox, so one GmailSearch is built and cached per account.
notmuch is a single account-agnostic index.

Falls back to notmuch whenever Gmail can't be used — a missing token or a
network failure should degrade prep, not break it.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _backend_for_account(account: Optional[str]) -> str:
    acct = (account or "").strip().lower()
    if acct:
        override = os.getenv(f"EMAIL_SEARCH_BACKEND_{acct.upper()}")
        if override:
            return override.strip().lower()
    return os.getenv("EMAIL_SEARCH_BACKEND", "notmuch").strip().lower()


class EmailSearchRouter:
    """Drop-in for `NotmuchSearch`, dispatching per account."""

    def __init__(self, default_account: Optional[str] = None):
        self.default_account = default_account
        self._notmuch: Any = None
        self._gmail_by_account: Dict[str, Any] = {}
        self._gmail_failed: set[str] = set()

    def _get_notmuch(self) -> Any:
        if self._notmuch is None:
            from notmuch_search import NotmuchSearch

            self._notmuch = NotmuchSearch()
        return self._notmuch

    def _get_gmail(self, account: str) -> Any:
        acct = (account or "").lower()
        if acct in self._gmail_by_account:
            return self._gmail_by_account[acct]
        from gmail_search import GmailSearch

        client = GmailSearch(account=acct)
        self._gmail_by_account[acct] = client
        return client

    def _backend(self, account: Optional[str]) -> Any:
        acct = (account or self.default_account or "").lower()
        if _backend_for_account(acct) != "gmail":
            return self._get_notmuch()
        if not acct:
            logger.warning("gmail backend requested without an account; using notmuch")
            return self._get_notmuch()
        if acct in self._gmail_failed:
            return self._get_notmuch()
        try:
            return self._get_gmail(acct)
        except Exception as exc:  # noqa: BLE001 - degrade rather than lose prep
            logger.warning(
                "gmail backend unavailable for %s (%s); falling back to notmuch",
                acct,
                exc,
            )
            self._gmail_failed.add(acct)
            return self._get_notmuch()

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
        kwargs = dict(
            query=query,
            limit=limit,
            account=account,
            date_filter=date_filter,
            date_from=date_from,
            date_to=date_to,
            tags=tags,
            sender=sender,
            recipient=recipient,
            subject=subject,
            include_body=include_body,
        )
        backend = self._backend(account)
        try:
            return backend.search_emails(**kwargs)
        except Exception as exc:  # noqa: BLE001
            acct = (account or self.default_account or "").lower()
            if backend is self._notmuch:
                logger.warning("email search failed: %s", exc)
                return []
            # A live Gmail call can fail on network or quota where the local
            # index would have answered. Retry once on notmuch before giving up.
            logger.warning("gmail search failed (%s); retrying on notmuch", exc)
            self._gmail_failed.add(acct)
            try:
                return self._get_notmuch().search_emails(**kwargs)
            except Exception as fallback_exc:  # noqa: BLE001
                logger.warning("notmuch fallback also failed: %s", fallback_exc)
                return []


def make_email_search(default_account: Optional[str] = None) -> EmailSearchRouter:
    return EmailSearchRouter(default_account=default_account)
