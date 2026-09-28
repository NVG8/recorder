#!/usr/bin/env python3

import json
import logging
import subprocess
from typing import Dict, List, Optional
import re

import recorder_config as rc

logger = logging.getLogger(__name__)


class NotmuchSearch:
    """Notmuch-based email search with structured queries."""
    
    def __init__(self, mail_dir: str | None = None):
        self.mail_dir = mail_dir or str(rc.MAIL_DIR)
        
    def _run_notmuch_command(self, args: List[str]) -> str:
        """Run a notmuch command and return output."""
        try:
            cmd = ["notmuch"] + args
            result = subprocess.run(
                cmd, 
                capture_output=True, 
                text=True, 
                cwd=self.mail_dir,
                timeout=30
            )
            if result.returncode != 0:
                logger.error(f"Notmuch command failed: {' '.join(cmd)}")
                logger.error(f"Error: {result.stderr}")
                return ""
            return result.stdout
        except subprocess.TimeoutExpired:
            logger.error(f"Notmuch command timed out: {' '.join(args)}")
            return ""
        except Exception as e:
            logger.error(f"Notmuch command failed: {e}")
            return ""
    
    def _parse_date_filter(self, date_str: str) -> str:
        """Convert various date formats to notmuch date syntax."""
        if not date_str:
            return ""
            
        # Handle relative dates
        date_lower = date_str.lower()
        if date_lower in ["today"]:
            return "date:today"
        elif date_lower in ["yesterday"]:
            return "date:yesterday"
        elif date_lower.endswith("d"):  # "7d", "30d"
            try:
                days = int(date_lower[:-1])
                return f"date:{days}d.."
            except ValueError:
                pass
        elif date_lower.endswith("w"):  # "1w", "2w"
            try:
                weeks = int(date_lower[:-1])
                return f"date:{weeks}w.."
            except ValueError:
                pass
        elif date_lower.endswith("m"):  # "1m", "3m"
            try:
                months = int(date_lower[:-1])
                return f"date:{months}M.."
            except ValueError:
                pass
        elif date_lower.endswith("y"):  # "1y"
            try:
                years = int(date_lower[:-1])
                return f"date:{years}y.."
            except ValueError:
                pass
        
        # Handle absolute dates
        if re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
            return f"date:{date_str}"
        
        # Handle date ranges
        if '..' in date_str:
            return f"date:{date_str}"
            
        return f"date:{date_str}"
    
    def _build_query(self, 
                     query: str = "*",
                     account: Optional[str] = None,
                     date_filter: Optional[str] = None,
                     date_from: Optional[str] = None,
                     date_to: Optional[str] = None,
                     tags: Optional[List[str]] = None,
                     sender: Optional[str] = None,
                     recipient: Optional[str] = None,
                     subject: Optional[str] = None) -> str:
        """Build a notmuch query from parameters."""
        parts = []
        
        # Add main query with proper parentheses for complex queries
        if query and query != "*":
            # If query contains OR operators, wrap in parentheses to ensure proper precedence
            if " OR " in query.upper():
                parts.append(f"({query})")
            else:
                parts.append(query)
        
        # Account filtering (maps to email addresses)
        if account:
            # Map account names to email addresses
            emails = rc.account_emails(account)
            if emails:
                # Search for emails from OR to any of this account's addresses
                clauses = []
                for email in emails:
                    clauses.append(f"from:{email}")
                    clauses.append(f"to:{email}")
                parts.append(f"({' OR '.join(clauses)})")
            
        # Date filtering
        if date_filter:
            parts.append(self._parse_date_filter(date_filter))
        elif date_from or date_to:
            if date_from and date_to:
                parts.append(f"date:{date_from}..{date_to}")
            elif date_from:
                parts.append(f"date:{date_from}..")
            elif date_to:
                parts.append(f"date:..{date_to}")
        
        # Tag filtering
        if tags and isinstance(tags, list):
            for tag in tags:
                parts.append(f"tag:{tag}")
        
        # Sender filtering
        if sender:
            parts.append(f"from:{sender}")
            
        # Recipient filtering  
        if recipient:
            parts.append(f"to:{recipient}")
            
        # Subject filtering
        if subject:
            parts.append(f"subject:{subject}")
        
        return " AND ".join(parts) if parts else "*"
    
    def search_emails(self,
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
                     include_body: bool = False) -> List[Dict]:
        """
        Search emails using notmuch with structured filters.
        
        Returns list of email dictionaries with metadata.
        """
        # Build notmuch query
        nm_query = self._build_query(
            query=query,
            account=account,
            date_filter=date_filter,
            date_from=date_from,
            date_to=date_to,
            tags=tags,
            sender=sender,
            recipient=recipient,
            subject=subject
        )
        
        logger.info(f"Notmuch query: {nm_query}")

        # Search for individual messages (not threads) so we see every
        # email in a conversation, including critical replies.
        msg_id_args = [
            "search",
            "--format=json",
            "--output=messages",
            f"--limit={limit}",
            nm_query,
        ]
        output = self._run_notmuch_command(msg_id_args)
        if not output.strip():
            return []

        try:
            message_ids = json.loads(output)
            if not isinstance(message_ids, list):
                return []
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse notmuch JSON output: {e}")
            return []

        # Fetch each message individually so we get per-message headers + body
        results = []
        for msg_id in message_ids:
            if not isinstance(msg_id, str):
                continue

            show_args = ["show", "--format=json", "--entire-thread=false", "--include-html", f"id:{msg_id}"]
            msg_output = self._run_notmuch_command(show_args)
            if not msg_output.strip():
                continue

            try:
                data = json.loads(msg_output)
                msg = self._find_first_message(data)
                if not msg:
                    continue

                headers = msg.get("headers", {})
                from_hdr = headers.get("From", "")
                to_hdr = headers.get("To", "")
                subject = headers.get("Subject", "")
                date_str = headers.get("Date", "")
                tags = msg.get("tags", [])

                email_data = {
                    "message_id": msg_id,
                    "subject": subject,
                    "sender": from_hdr,
                    "recipient": to_hdr,
                    "date": date_str,
                    "tags": tags,
                    "filename": msg.get("filename", [""])[0] if isinstance(msg.get("filename"), list) else msg.get("filename", ""),
                    "account": self._extract_account_from_authors([from_hdr]),
                    "similarity_score": 1.0,
                }

                if include_body:
                    body_parts = msg.get("body", [])
                    body_text = ""
                    for part in body_parts:
                        body_text = self._extract_text_from_body(part)
                        if body_text:
                            break
                    email_data["body_text"] = body_text

                results.append(email_data)
            except (json.JSONDecodeError, KeyError, IndexError) as e:
                logger.warning(f"Failed to parse message {msg_id}: {e}")
                continue

        return results

    @staticmethod
    def _find_first_message(data) -> Optional[Dict]:
        """Recurse into notmuch's nested list structure to find the message dict."""
        if isinstance(data, dict) and "headers" in data:
            return data
        if isinstance(data, list):
            for item in data:
                result = NotmuchSearch._find_first_message(item)
                if result:
                    return result
        return None

    def _extract_account_from_authors(self, authors: List[str]) -> str:
        """Extract account name from thread authors (fallback method)."""
        if not authors:
            return 'unknown'
        author_text = ' '.join(authors).lower()
        for account in rc.ACCOUNTS:
            if any(e.lower() in author_text for e in rc.account_emails(account)):
                return account
        if rc.USER_NAME.lower() in author_text:
            return rc.DEFAULT_ACCOUNT
        return 'unknown'

    def _extract_text_from_body(self, body_part: Dict) -> str:
        """Extract text content from notmuch body structure."""
        if not isinstance(body_part, dict):
            return ""
        
        content_type = body_part.get('content-type', '')
        if 'text/plain' in content_type:
            content = body_part.get('content', '')
            if isinstance(content, list):
                return '\n'.join(content)
            return str(content)
        elif 'multipart' in content_type:
            # Handle multipart messages
            content = body_part.get('content', [])
            if isinstance(content, list):
                texts = []
                for part in content:
                    text = self._extract_text_from_body(part)
                    if text:
                        texts.append(text)
                return '\n'.join(texts)
        
        return ""
