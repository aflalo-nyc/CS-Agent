"""The service's plumbing: signed links, the go-live cutoff, the draft that gets placed in
Gmail, and the click-twice guard. No network: IMAP is faked, the brain is not involved."""

from __future__ import annotations

import sqlite3

import pytest

from aflalo_cs import service
from aflalo_cs.inbox_import import FetchedEmail
from aflalo_cs.store import Store

SECRET = "test-secret"


def _msg(mid, thread, received, direction="inbound", sender="ana@example.com", subject="order 7412"):
    return FetchedEmail(
        message_id=mid, thread_id=thread, sender=sender, subject=subject,
        body="hi, where is my order?", received_at=received, is_unread=True,
        to_addr="aflalo@aflalonyc.com", direction=direction,
    )


def test_signed_link_round_trips_and_rejects_tampering():
    link = service.draft_link("abc@mail.example", portal_url="https://cs.example", secret=SECRET)
    assert link.startswith("https://cs.example/draft/")
    sig, mid = link.rsplit("/", 2)[-2:]
    assert service.link_ok(sig, "abc@mail.example", SECRET)
    assert not service.link_ok(sig, "other@mail.example", SECRET)
    assert not service.link_ok(sig[::-1], "abc@mail.example", SECRET)


def test_golive_is_recorded_once_and_reused(tmp_path, monkeypatch):
    monkeypatch.delenv("CS_GOLIVE_AT", raising=False)
    db = str(tmp_path / "cs.db")
    first = service.golive_at(db)
    assert service.golive_at(db) == first
    monkeypatch.setenv("CS_GOLIVE_AT", "2030-01-01T00:00:00+00:00")
    assert service.golive_at(db) == "2030-01-01T00:00:00+00:00"


def test_cutoff_mailbox_hides_mail_from_before_golive(tmp_path):
    store = Store(tmp_path / "cs.db")
    store.save_inbox([
        _msg("old@x", "t1", "2026-09-01T10:00:00+00:00"),
        _msg("new@x", "t2", "2026-10-02T10:00:00+00:00"),
    ])
    box = service.CutoffMailbox(store, since="2026-10-01T00:00:00+00:00")
    ids = [e.message_id for e in box.fetch_unprocessed()]
    assert ids == ["new@x"]


def test_build_draft_threads_under_her_message():
    row = {"message_id": "her-id@mail.example", "sender": "Ana <ana@example.com>",
           "subject": "order 7412", "thread_id": "123", "draft_text": "Hi Ana,\n\nWarmly,\nEva"}
    mime = service.build_draft(row, from_addr="aflalo@aflalonyc.com")
    assert mime["To"] == "Ana <ana@example.com>"
    assert mime["Subject"] == "Re: order 7412"
    assert mime["In-Reply-To"] == "<her-id@mail.example>"
    assert mime["References"] == "<her-id@mail.example>"
    assert mime.get_body(("plain",)).get_content().strip().endswith("Eva")


def test_gmail_thread_url_uses_hex_thread_id_or_message_search():
    assert service.gmail_thread_url({"thread_id": "255", "message_id": "m"}, "a@b.co").endswith("#all/ff")
    url = service.gmail_thread_url({"thread_id": "", "message_id": "x@y"}, "a@b.co")
    assert "rfc822msgid" in url


class FakeIMAP:
    appended: list = []

    def __init__(self, host):
        self.host = host

    def login(self, user, pw):
        assert pw == "app-password"

    def append(self, folder, flags, date, data):
        FakeIMAP.appended.append((folder, flags, data))
        return "OK", [b"APPEND completed"]

    def logout(self):
        pass


def test_place_draft_once_then_already(tmp_path):
    db = str(tmp_path / "cs.db")
    Store(db)
    FakeIMAP.appended.clear()
    row = {"message_id": "her-id@mail.example", "sender": "ana@example.com", "subject": "hi",
           "thread_id": "9", "draft_text": "Hi Ana"}
    kw = dict(imap_user="aflalo@aflalonyc.com", imap_password="app-password", imap_factory=FakeIMAP)
    assert service.place_draft(db, row, **kw) == "placed"
    assert service.place_draft(db, row, **kw) == "already"
    assert len(FakeIMAP.appended) == 1
    folder, flags, data = FakeIMAP.appended[0]
    assert folder == '"[Gmail]/Drafts"' and flags == r"(\Draft)" and b"In-Reply-To" in data
    # an edited draft is a new draft
    row["draft_text"] = "Hi Ana, updated"
    assert service.place_draft(db, row, **kw) == "placed"
    assert len(FakeIMAP.appended) == 2


def test_latest_draft_reads_newest_text(tmp_path):
    db = str(tmp_path / "cs.db")
    store = Store(db)
    store.save_inbox([_msg("m1@x", "t", "2026-10-02T10:00:00+00:00")])
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO drafts (message_id, draft_text, created_at) VALUES ('m1@x','first','t1')")
        c.execute("INSERT INTO drafts (message_id, draft_text, created_at) VALUES ('m1@x','second','t2')")
    row = service.latest_draft(db, "m1@x")
    assert row["draft_text"] == "second" and row["sender"] == "ana@example.com"
    assert service.latest_draft(db, "nope") is None
