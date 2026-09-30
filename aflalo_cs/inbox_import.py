"""Read-only import of the shared CS inbox, as whole conversations.

This module CANNOT modify the mailbox. Enforced, not promised:

1. IMAP is opened with `readonly=True` (EXAMINE, not SELECT), so the server does not set
   the \\Seen flag and fetching does not mark anything read.
2. Every fetch uses BODY.PEEK[] — belt and braces on servers that would set \\Seen anyway.
3. The only verbs used are LOGIN, LIST, EXAMINE, SEARCH, FETCH, LOGOUT. There is no code
   path here that creates a label, creates a draft, or sends anything.

Both INBOX and Sent Mail are read, because a conversation is the unit that matters — see
`threads` in DATA_MODEL.md. Everything lands in local SQLite; drafting runs against that
copy, so the real mailbox is never touched again after the read.

    python -m aflalo_cs.inbox_import --user aflalo@aflalonyc.com --days 30 --dry-run

Auth is a Gmail App Password, prompted for and never stored. Set AFLALO_IMAP_PASSWORD to
run without a prompt (scheduled runs, or a shell with no tty).
"""

from __future__ import annotations

import argparse
import email
import email.policy
import email.utils
import getpass
import imaplib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import Email

IMAP_HOST = "imap.gmail.com"
READONLY_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
SENT_FOLDERS = ("[Gmail]/Sent Mail", "[Google Mail]/Sent Mail", "Sent")

# X-GM-THRID is Gmail's own conversation id. Far more reliable than reconstructing threads
# from References/In-Reply-To, which break whenever a mail client rewrites them.
THRID_RE = re.compile(rb"X-GM-THRID (\d+)")

# Shopify sends order notifications FROM the store's own address TO the shared inbox, so a
# single notification lands in INBOX *and* in Sent. NOISE_SENDERS can't catch them — the
# sender is us. Measured against 30 days of the real mailbox they were 276 of 490
# "conversations", and 276 of 399 messages in the voice corpus: the drafter would have
# learned its tone mostly from a Shopify template.
#
# Matched on subject rather than sender precisely because the sender is legitimate. Kept
# deliberately narrow — "Re: Sizing question" sent from the same address with no inbound
# half is a real human reply whose customer message fell outside the import window, and
# that is exactly the gold-standard writing we want to keep.
NOISE_SUBJECTS = re.compile(r"^\s*\[AFLALO\]\s+Order\s+#?\d+\s+placed by", re.IGNORECASE)

NOISE_SENDERS = re.compile(
    r"noreply|no-reply|notification|mailer-daemon|postmaster|@shopify\.com|@slack\.com|"
    r"@notion\.so|@google\.com|@klaviyo|@stripe\.com|calendar-",
    re.IGNORECASE,
)


@dataclass
class FetchedEmail:
    message_id: str
    thread_id: str
    sender: str
    subject: str
    body: str
    received_at: str
    is_unread: bool
    to_addr: str
    direction: str = "inbound"  # inbound = customer wrote in; outbound = we replied
    folder: str = "INBOX"

def _body_of(msg: email.message.Message) -> str:
    """Prefer text/plain; fall back to stripping tags off the HTML part."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(
                part.get("Content-Disposition", "")
            ):
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", "replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    html = payload.decode(part.get_content_charset() or "utf-8", "replace")
                    return re.sub(r"<[^>]+>", " ", html)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(msg.get_content_charset() or "utf-8", "replace")
    return ""


QUOTE_MARKERS = re.compile(
    # Gmail/Apple Mail attribution lines. Deliberately NOT anchored to a newline: HTML
    # bodies flatten newlines to spaces, and "…grateful to you. Jen  On Aug 26, 2026, at
    # 9:22 AM, AFLALO NYC <…> wrote:" must still cut. Requires the full date shape so a
    # sentence like "on Friday we wrote:" can't false-positive.
    r"\s(?:>+ ?)?On (?:[A-Z][a-z]{2}, )?[A-Z][a-z]{2,8}\.? \d{1,2}, \d{4},? (?:at )?\d{1,2}:\d{2}"
    r".{0,120}?wrote:"
    r"|\n-{2,} ?Original Message|\n_{5,}|\nFrom: .*\nSent: "
    r"|\s-{5,} Forwarded message -{5,}",
    re.DOTALL,
)


def _strip_quoted(body: str) -> str:
    """Drop quoted history — we store each message once and rebuild the thread ourselves."""
    text = body.replace("\xa0", " ").replace("&nbsp;", " ")
    m = QUOTE_MARKERS.search(text)
    cut = text[: m.start()] if m else text
    return re.sub(r"\n{3,}", "\n\n", cut).strip()


def _iso(date_header: str) -> str:
    """RFC-2822 date -> ISO-8601, so threads sort correctly in SQL."""
    try:
        return email.utils.parsedate_to_datetime(date_header).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return ""


def _read_folder(
    conn: imaplib.IMAP4_SSL, folder: str, *, days: int, limit: int, direction: str
) -> list[FetchedEmail]:
    status, _ = conn.select(f'"{folder}"', readonly=True)
    if status != "OK":
        return []

    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
    status, data = conn.search(None, f'(SINCE "{since}")')
    if status != "OK" or not data or not data[0]:
        return []

    out: list[FetchedEmail] = []
    for uid in data[0].split()[-limit:]:
        status, raw = conn.fetch(uid, "(FLAGS X-GM-THRID BODY.PEEK[])")
        if status != "OK" or not raw or not isinstance(raw[0], tuple):
            continue

        meta, body_bytes = raw[0][0], raw[0][1]
        flags = meta.decode("utf-8", "replace")
        thrid = THRID_RE.search(meta)
        msg = email.message_from_bytes(body_bytes, policy=email.policy.default)

        sender = str(msg.get("From", ""))
        # Direction by folder alone misfiles our own replies: copies of team mail land in
        # INBOX (client save-behavior, CCs to ourselves) and would count as customer
        # messages — polluting the queue AND the response-time clocks. The From header is
        # authoritative: our address wrote it, it's outbound.
        msg_direction = "outbound" if "aflalo@aflalonyc.com" in sender.lower() else direction
        if msg_direction == "inbound" and NOISE_SENDERS.search(sender):
            continue
        # Both directions: the notification exists as an inbound and an outbound copy, and
        # dropping only one half leaves a half-conversation behind.
        if NOISE_SUBJECTS.match(str(msg.get("Subject", ""))):
            continue

        mid = str(msg.get("Message-ID", f"uid-{folder}-{uid.decode()}")).strip("<>")
        refs = str(msg.get("References", "") or "").split()
        thread_id = thrid.group(1).decode() if thrid else (refs[0].strip("<>") if refs else mid)

        out.append(
            FetchedEmail(
                message_id=mid,
                thread_id=thread_id,
                sender=sender,
                subject=str(msg.get("Subject", "")),
                body=_strip_quoted(_body_of(msg)),
                received_at=_iso(str(msg.get("Date", ""))),
                is_unread="\\Seen" not in flags,
                to_addr=str(msg.get("To", "")),
                direction=msg_direction,
                folder=folder,
            )
        )
    return out


class MailboxAuthError(RuntimeError):
    """Login was refused. Carries the fix, not just the rejection."""


LOGIN_HELP = (
    (
        "application-specific password required",
        "This account has 2-step verification on, so IMAP will not accept the normal\n"
        "account password. Generate an App Password instead:\n"
        "  https://myaccount.google.com/apppasswords\n"
        "It is 16 lowercase letters, shown once. Use that as the password here.",
    ),
    (
        "invalid credentials",
        "Username or password rejected. Check the address, and note that an App Password\n"
        "is 16 lowercase letters with no spaces — not the account password.",
    ),
    (
        "imap access is disabled",
        "IMAP is switched off for this mailbox. A Workspace admin enables it under\n"
        "Apps -> Google Workspace -> Gmail -> End User Access.",
    ),
)


def _explain_login_failure(message: str) -> str:
    lowered = message.lower()
    for needle, help_text in LOGIN_HELP:
        if needle in lowered:
            return help_text
    return message


def fetch_readonly(
    user: str, app_password: str, *, days: int = 30, limit: int = 200
) -> list[FetchedEmail]:
    """Read INBOX *and* Sent Mail, so we capture conversations rather than half of each.

    The sent side matters twice over: the drafter has to know what we already told her (the
    deck is explicit — don't explain the policy twice), and real sent replies are the
    gold-standard voice reference the design doc §3.3 asks for.
    """
    conn = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        try:
            conn.login(user, app_password)
        except imaplib.IMAP4.error as exc:
            detail = exc.args[0].decode("utf-8", "replace") if exc.args and isinstance(exc.args[0], bytes) else str(exc)
            raise MailboxAuthError(_explain_login_failure(detail)) from None
        msgs = _read_folder(conn, "INBOX", days=days, limit=limit, direction="inbound")
        for folder in SENT_FOLDERS:
            got = _read_folder(conn, folder, days=days, limit=limit, direction="outbound")
            if got:
                msgs += got
                break
        return msgs
    finally:
        conn.logout()


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only import of the CS inbox")
    ap.add_argument("--user", required=True, help="e.g. aflalo@aflalonyc.com")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--dry-run", action="store_true", help="show what was read, store nothing")
    args = ap.parse_args()

    print("READ-ONLY IMPORT — INBOX + Sent Mail")
    print("  opened with EXAMINE, fetched with BODY.PEEK — nothing is marked read")
    print("  no labels created · no labels applied · no drafts · nothing sent\n")

    # Prompt by default so the password never lands in shell history. AFLALO_IMAP_PASSWORD
    # exists for the non-interactive cases — a scheduled run, or a terminal without a tty.
    pw = os.environ.get("AFLALO_IMAP_PASSWORD")
    if pw:
        print(f"using AFLALO_IMAP_PASSWORD for {args.user}\n")
    else:
        try:
            pw = getpass.getpass(f"App Password for {args.user} (not echoed, never stored): ")
        except (EOFError, OSError):
            print(
                "No terminal to prompt on. Either run this from a shell, or set\n"
                "  AFLALO_IMAP_PASSWORD=<app password>  and run it again."
            )
            return 2
    if not pw:
        print("No password given. Nothing read.")
        return 2
    try:
        mail = fetch_readonly(args.user, pw, days=args.days, limit=args.limit)
    except MailboxAuthError as exc:
        print(f"Could not sign in as {args.user}.\n\n{exc}")
        return 2

    inbound = [m for m in mail if m.direction == "inbound"]
    outbound = [m for m in mail if m.direction == "outbound"]
    threads = {m.thread_id for m in mail}
    print(
        f"read {len(mail)} messages across {len(threads)} conversations — "
        f"{len(inbound)} inbound, {len(outbound)} replies "
        f"({sum(m.is_unread for m in inbound)} still unread, left that way)\n"
    )

    for m in sorted(mail, key=lambda x: x.received_at, reverse=True)[:15]:
        arrow = "←" if m.direction == "inbound" else "→"
        print(f"  {arrow} {m.received_at[:19]:<21}{m.sender[:34]:<36}{m.subject[:42]}")
    if len(mail) > 15:
        print(f"  … and {len(mail) - 15} more")

    if args.dry_run:
        print("\n--dry-run: nothing stored.")
        return 0

    from . import config
    from .store import Store

    store = Store(config.DB_PATH)
    n = store.save_inbox(mail)
    print(f"\nstored {n} messages in {config.DB_PATH}")
    print("Next: python -m aflalo_cs.cli run --from-store")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
