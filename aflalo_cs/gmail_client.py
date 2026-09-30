"""[1] Ingestion and [9] Draft Writer.

The Gmail OAuth scopes below deliberately exclude `gmail.send`. That is a structural safety
boundary, not a convention we have to remember: even a bug in the pipeline logic cannot email
a customer directly, because the credential has no authority to send.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Protocol

from .models import LABELS, PHISHING_LABEL, Email

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.compose",  # create drafts; does NOT permit send
]

PROCESSING_LABELS = [*LABELS.values(), PHISHING_LABEL]


class Mailbox(Protocol):
    def fetch_unprocessed(self, limit: int) -> list[Email]: ...
    def create_draft(self, email: Email, body: str, cc: str | None = None) -> str: ...
    def apply_label(self, email: Email, label: str) -> None: ...


def _html_body(body: str) -> str:
    """A minimal HTML alternative so links are clickable in the Gmail UI.

    Review feedback: the tracking URL should live ON the tracking number. The drafter
    writes "Tracking 123456: https://…"; here the number becomes the anchor and the bare
    URL disappears. Any other URL becomes a plain clickable link.
    """
    import html as _html
    import re as _re

    text = _html.escape(body)
    anchors: list[str] = []

    def _hold(anchor: str) -> str:
        anchors.append(anchor)
        return f"\x00{len(anchors) - 1}\x00"

    # Pass 1: tracking lines — the number becomes the link, the bare URL disappears.
    text = _re.sub(
        r"Tracking (\S+): (https?://\S+)",
        lambda m: "Tracking " + _hold(f'<a href="{m.group(2)}">{m.group(1)}</a>'),
        text,
    )
    # Pass 2: any remaining URL becomes a plain clickable link.
    text = _re.sub(
        r"https?://[^\s<]+",
        lambda m: _hold(f'<a href="{m.group(0)}">{m.group(0)}</a>'),
        text,
    )
    for i, anchor in enumerate(anchors):
        text = text.replace(f"\x00{i}\x00", anchor)
    return "<div>" + text.replace("\n", "<br>\n") + "</div>"


@dataclass
class MockMailbox:
    """Seeded mock inbox — same code path, different mailbox."""

    path: Path
    drafts: dict[str, str] = field(default_factory=dict)
    labels: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        raw = json.loads(Path(self.path).read_text())
        self._emails = [Email(**e) for e in raw]

    def fetch_unprocessed(self, limit: int = 50) -> list[Email]:
        return [e for e in self._emails if not self.labels.get(e.message_id)][:limit]

    def create_draft(self, email: Email, body: str, cc: str | None = None) -> str:
        self.drafts[email.message_id] = body
        self.ccs = getattr(self, "ccs", {})
        self.ccs[email.message_id] = cc
        return f"draft_{email.message_id}"

    def apply_label(self, email: Email, label: str) -> None:
        self.labels.setdefault(email.message_id, []).append(label)


@dataclass
class GmailMailbox:
    """Live Gmail. Polling, not push — a plain search query is enough at CS-inbox volume."""

    credentials_path: str = "credentials.json"
    token_path: str = "token.json"
    user_id: str = "me"
    _service: Any = None

    def __post_init__(self) -> None:
        if self._service is None:
            self._service = self._build_service()
        self._label_ids = self._ensure_labels()

    def _build_service(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = None
        if Path(self.token_path).exists():
            creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_path, SCOPES)
                creds = flow.run_local_server(port=0)
            Path(self.token_path).write_text(creds.to_json())
        return build("gmail", "v1", credentials=creds)

    def _ensure_labels(self) -> dict[str, str]:
        existing = {
            l["name"]: l["id"]
            for l in self._service.users().labels().list(userId=self.user_id).execute().get("labels", [])
        }
        for name in PROCESSING_LABELS:
            if name not in existing:
                created = (
                    self._service.users()
                    .labels()
                    .create(userId=self.user_id, body={"name": name})
                    .execute()
                )
                existing[name] = created["id"]
        return {n: existing[n] for n in PROCESSING_LABELS}

    def fetch_unprocessed(self, limit: int = 50) -> list[Email]:
        query = "in:inbox -in:draft " + " ".join(f'-label:"{l}"' for l in PROCESSING_LABELS)
        resp = (
            self._service.users()
            .messages()
            .list(userId=self.user_id, q=query, maxResults=limit)
            .execute()
        )
        out: list[Email] = []
        for ref in resp.get("messages", []):
            msg = (
                self._service.users()
                .messages()
                .get(userId=self.user_id, id=ref["id"], format="full")
                .execute()
            )
            headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
            out.append(
                Email(
                    message_id=msg["id"],
                    thread_id=msg["threadId"],
                    sender=headers.get("from", ""),
                    subject=headers.get("subject", ""),
                    body=_extract_body(msg["payload"]),
                )
            )
        return out

    def create_draft(self, email: Email, body: str, cc: str | None = None) -> str:
        mime = EmailMessage()
        mime["To"] = email.sender
        if cc:
            mime["Cc"] = cc
        mime["Subject"] = email.subject if email.subject.lower().startswith("re:") else f"Re: {email.subject}"
        mime.set_content(body)
        mime.add_alternative(_html_body(body), subtype="html")
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        created = (
            self._service.users()
            .drafts()
            .create(
                userId=self.user_id,
                body={"message": {"raw": raw, "threadId": email.thread_id}},
            )
            .execute()
        )
        return created["id"]

    def apply_label(self, email: Email, label: str) -> None:
        self._service.users().messages().modify(
            userId=self.user_id,
            id=email.message_id,
            body={"addLabelIds": [self._label_ids[label]]},
        ).execute()


def _extract_body(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        text = _extract_body(part)
        if text:
            return text
    return ""


@dataclass
class StoredMailbox:
    """Reads the read-only import from SQLite and keeps drafts local.

    Nothing here can reach Gmail — there is no service object and no credential. Use this to
    practise drafting against real customer mail with the real mailbox untouched.
    """

    store: Any
    drafts: dict[str, str] = field(default_factory=dict)
    labels: dict[str, list[str]] = field(default_factory=dict)

    def fetch_unprocessed(self, limit: int = 200) -> list[Email]:
        return self.store.inbox(limit)

    def create_draft(self, email: Email, body: str, cc: str | None = None) -> str:
        self.drafts[email.message_id] = body
        return f"local_{email.message_id}"

    def apply_label(self, email: Email, label: str) -> None:
        self.labels.setdefault(email.message_id, []).append(label)
