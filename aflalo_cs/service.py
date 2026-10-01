"""The always-on service (Railway). Plumbing only — the brain is untouched.

    python -m aflalo_cs.service        serve on $PORT; every CS_INTERVAL_MIN minutes run one cycle

One cycle, in order:
  1. IMPORT   read INBOX + Sent over IMAP, read-only (inbox_import.fetch_readonly) → SQLite
  2. DRAFT    run the existing Pipeline against the local copy (StoredMailbox) for customer
              messages received after go-live; drafts land in SQLite, never in Gmail
  3. PUSH     sync CS Drafts / CS Threads / CS KPI Summary to Airtable (airtable.sync_store)

Routes:
  GET /health                      "ok"
  GET /draft/<sig>/<message_id>    THE BUTTON. Places this message's latest draft into the
                                   Gmail Drafts folder, threaded under her email, then
                                   redirects to that thread in Gmail. Idempotent: clicking
                                   twice does not create a second draft.

The link in Airtable carries an HMAC of the message id (CS_LINK_SECRET) so a leaked row can't
be turned into a draft-for-any-message endpoint. Placing a draft uses the same IMAP app
password as the import: APPEND to [Gmail]/Drafts. No Google OAuth, no send scope — the only
thing this process can add to the mailbox is a draft, and only when a person clicks.

Go-live cutoff: the first time the service starts it records the moment (service_meta
'golive_at'); only customer messages received after that are drafted, so the backlog of
already-answered or long-dead threads is never touched. Override with CS_GOLIVE_AT (ISO 8601).
"""

from __future__ import annotations

import email.utils
import hashlib
import hmac
import html
import imaplib
import logging
import os
import sqlite3
import threading
import time
import traceback
from datetime import datetime, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

from . import config
from .gmail_client import StoredMailbox, _html_body
from .store import Store

log = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
DRAFTS_FOLDERS = ("[Gmail]/Drafts", "[Google Mail]/Drafts", "Drafts")
IMAP_USER = os.environ.get("AFLALO_IMAP_USER", "aflalo@aflalonyc.com")
PORTAL_URL = os.environ.get("CS_PORTAL_URL", "").rstrip("/")
LINK_SECRET = os.environ.get("CS_LINK_SECRET", "")


# ------------------------------------------------------------------ signed links

def sign(message_id: str, secret: str | None = None) -> str:
    key = (secret if secret is not None else LINK_SECRET).encode()
    return hmac.new(key, message_id.encode(), hashlib.sha256).hexdigest()[:20]


def draft_link(message_id: str, portal_url: str | None = None, secret: str | None = None) -> str:
    base = (portal_url if portal_url is not None else PORTAL_URL).rstrip("/")
    return f"{base}/draft/{sign(message_id, secret)}/{quote(message_id, safe='')}"


def link_ok(sig: str, message_id: str, secret: str | None = None) -> bool:
    return hmac.compare_digest(sig, sign(message_id, secret))


# ------------------------------------------------------------------ service-side tables

META_SQL = """
CREATE TABLE IF NOT EXISTS service_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gmail_drafts_placed (
    message_id TEXT PRIMARY KEY,
    draft_hash TEXT NOT NULL,
    placed_at  TEXT NOT NULL
);
"""


def _db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(META_SQL)
    return conn


def golive_at(db_path: str) -> str:
    """ISO timestamp before which nothing is drafted. Env wins; else first-start, persisted."""
    env = os.environ.get("CS_GOLIVE_AT")
    if env:
        return env
    with _db(db_path) as c:
        row = c.execute("SELECT value FROM service_meta WHERE key='golive_at'").fetchone()
        if row:
            return row["value"]
        now = datetime.now(timezone.utc).isoformat()
        c.execute("INSERT INTO service_meta (key, value) VALUES ('golive_at', ?)", (now,))
        return now


class CutoffMailbox(StoredMailbox):
    """StoredMailbox, minus anything that arrived before go-live. Plumbing, not routing:
    the Pipeline still decides everything about the messages it is shown."""

    def __init__(self, store: Store, since: str) -> None:
        super().__init__(store=store)
        self.since = since

    def fetch_unprocessed(self, limit: int = 200) -> list:
        emails = super().fetch_unprocessed(limit)
        if not emails:
            return []
        ids = [e.message_id for e in emails]
        with sqlite3.connect(self.store.path) as c:
            q = ",".join("?" * len(ids))
            fresh = {
                r[0]
                for r in c.execute(
                    f"SELECT message_id FROM inbox_messages WHERE message_id IN ({q}) "
                    "AND received_at >= ?", (*ids, self.since),
                )
            }
        return [e for e in emails if e.message_id in fresh]


# ------------------------------------------------------------------ the cycle

def run_cycle(store: Store, *, imap_user: str, imap_password: str, out=print) -> None:
    from .airtable import sync_store
    from .inbox_import import fetch_readonly
    from .llm import AnthropicLLM
    from .pipeline import Pipeline

    days = int(os.environ.get("CS_IMPORT_DAYS", "30"))
    import_limit = int(os.environ.get("CS_IMPORT_LIMIT", "300"))
    draft_limit = int(os.environ.get("CS_DRAFT_LIMIT", "20"))

    # 1. import (read-only)
    mail = fetch_readonly(imap_user, imap_password, days=days, limit=import_limit)
    store.save_inbox(mail)
    out(f"agent: imported {len(mail)} messages (read-only)")

    # 2. draft, against the local copy, new mail only
    since = golive_at(store.path)
    shop, shop_mode = config.get_shopify()
    pipe = Pipeline(mailbox=CutoffMailbox(store, since), llm=AnthropicLLM(), shop=shop, store=store)
    outcomes = pipe.run(limit=draft_limit)
    if outcomes:
        counts: dict[str, int] = {}
        for o in outcomes:
            counts[o.decision.value] = counts.get(o.decision.value, 0) + 1
        out(f"agent: drafted {len(outcomes)} new (" + ", ".join(f"{v} {k}" for k, v in counts.items()) + f") shopify={shop_mode}")
    else:
        out(f"agent: nothing new since go-live ({since[:19]})")

    # 3. push to Airtable, with the button link on every row that has a draft
    token, base = os.environ.get("AIRTABLE_TOKEN"), os.environ.get("AIRTABLE_BASE")
    if not (token and base):
        out("agent: AIRTABLE_TOKEN / AIRTABLE_BASE not set, skipping push")
        return
    link = (lambda mid: draft_link(mid)) if (PORTAL_URL and LINK_SECRET) else None
    for line in sync_store(token, base, store, link_for=link):
        out("agent: " + line)


def cycle_loop(store: Store) -> None:
    minutes = float(os.environ.get("CS_INTERVAL_MIN", "10"))
    pw = os.environ.get("AFLALO_IMAP_PASSWORD", "")
    print(f"agent: every {minutes:g} min, inbox={IMAP_USER}, drafts go to Airtable only", flush=True)
    while True:
        if not pw:
            print("agent: AFLALO_IMAP_PASSWORD not set — cycle skipped (forms/health still up)", flush=True)
        else:
            try:
                run_cycle(store, imap_user=IMAP_USER, imap_password=pw, out=lambda s: print(s, flush=True))
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        time.sleep(minutes * 60)


# ------------------------------------------------------------------ the button

def latest_draft(db_path: str, message_id: str) -> dict | None:
    """Everything needed to place the draft: her message + the newest draft text."""
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            """
            SELECT i.message_id, i.thread_id, i.sender, i.subject, i.received_at,
                   (SELECT d.draft_text FROM drafts d WHERE d.message_id = i.message_id
                     ORDER BY d.rowid DESC LIMIT 1) AS draft_text
            FROM inbox_messages i WHERE i.message_id = ?
            """,
            (message_id,),
        ).fetchone()
    return dict(row) if row else None


def build_draft(row: dict, *, from_addr: str) -> EmailMessage:
    """A reply draft that Gmail threads under her message: In-Reply-To + References carry
    her Message-ID, the subject gets 'Re:', To is her address."""
    mime = EmailMessage()
    mime["From"] = from_addr
    mime["To"] = row["sender"]
    subj = row.get("subject") or ""
    mime["Subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}"
    mid = row["message_id"]
    if mid and not mid.startswith("uid-"):
        mime["In-Reply-To"] = f"<{mid}>"
        mime["References"] = f"<{mid}>"
    mime["Date"] = email.utils.formatdate(localtime=False)
    body = row["draft_text"] or ""
    mime.set_content(body)
    mime.add_alternative(_html_body(body), subtype="html")
    return mime


def gmail_thread_url(row: dict, user: str) -> str:
    tid = str(row.get("thread_id") or "")
    base = f"https://mail.google.com/mail/?authuser={quote(user)}"
    if tid.isdigit():
        return f"{base}#all/{int(tid):x}"
    return f"{base}#search/{quote('rfc822msgid:' + row['message_id'], safe='')}"


def _draft_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


def place_draft(db_path: str, row: dict, *, imap_user: str, imap_password: str, imap_factory=None) -> str:
    """APPEND the draft to Gmail's Drafts folder. Returns 'placed' or 'already'."""
    h = _draft_hash(row["draft_text"])
    with _db(db_path) as c:
        prev = c.execute(
            "SELECT draft_hash FROM gmail_drafts_placed WHERE message_id = ?", (row["message_id"],)
        ).fetchone()
        if prev and prev["draft_hash"] == h:
            return "already"

    mime = build_draft(row, from_addr=imap_user)
    conn = (imap_factory or imaplib.IMAP4_SSL)(IMAP_HOST)
    try:
        conn.login(imap_user, imap_password)
        last_err = None
        for folder in DRAFTS_FOLDERS:
            status, detail = conn.append(f'"{folder}"', r"(\Draft)", imaplib.Time2Internaldate(time.time()), mime.as_bytes())
            if status == "OK":
                break
            last_err = detail
        else:
            raise RuntimeError(f"could not append to any Drafts folder: {last_err}")
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass

    with _db(db_path) as c:
        c.execute(
            "INSERT OR REPLACE INTO gmail_drafts_placed (message_id, draft_hash, placed_at) VALUES (?,?,?)",
            (row["message_id"], h, datetime.now(timezone.utc).isoformat()),
        )
    return "placed"


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>body{{font:16px/1.5 -apple-system,Helvetica,Arial,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1.25rem;color:#1f1e1b}}
pre{{white-space:pre-wrap;background:#f4f3ef;padding:1rem;border-radius:8px}}a{{color:#3b4f8a}}</style>
<h1>{title}</h1>{body}"""


def make_handler(db_path: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # one line per request, like the pull portal
            print("%s \"%s\" %s" % (self.address_string(), fmt % args, ""), flush=True)

        def _send(self, code: int, body: str, headers: dict | None = None) -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._send(200, "ok")
                return
            parts = path.split("/")
            if len(parts) == 4 and parts[1] == "draft":
                from urllib.parse import unquote
                sig, mid = parts[2], unquote(parts[3])
                if not LINK_SECRET or not link_ok(sig, mid):
                    self._send(403, PAGE.format(title="Link not valid", body="<p>This link is not valid. Open the row in Airtable and click its Open in Gmail link again.</p>"))
                    return
                row = latest_draft(db_path, mid)
                if not row or not row.get("draft_text"):
                    self._send(404, PAGE.format(title="No draft for this email", body="<p>The agent wrote no draft for this message (see its Why column), so there is nothing to place in Gmail.</p>"))
                    return
                pw = os.environ.get("AFLALO_IMAP_PASSWORD", "")
                try:
                    if not pw:
                        raise RuntimeError("AFLALO_IMAP_PASSWORD is not set on the service")
                    result = place_draft(db_path, row, imap_user=IMAP_USER, imap_password=pw)
                except Exception as exc:  # noqa: BLE001
                    # Fail soft: the draft is still right here to copy.
                    self._send(500, PAGE.format(
                        title="Could not place the draft in Gmail",
                        body=f"<p>{html.escape(str(exc))}</p><p>Here is the draft to copy by hand:</p><pre>{html.escape(row['draft_text'])}</pre>"))
                    return
                print(f"button: {result} draft for {mid} -> {row['sender']}", flush=True)
                self._send(302, "", {"Location": gmail_thread_url(row, IMAP_USER)})
                return
            self._send(404, PAGE.format(title="Not found", body="<p>Nothing here.</p>"))

    return Handler


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    store = Store(config.DB_PATH)
    _db(store.path).close()
    if not LINK_SECRET:
        print("agent: CS_LINK_SECRET not set — Airtable rows will carry no Open in Gmail link", flush=True)
    if os.environ.get("CS_INTERVAL_MIN", "10") != "0":
        threading.Thread(target=cycle_loop, args=(store,), daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    print(f"cs agent on http://0.0.0.0:{port}  (health at /health, button at /draft/<sig>/<id>)  db={store.path}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(store.path)).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
