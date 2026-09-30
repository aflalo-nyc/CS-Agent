"""SQLite audit trail + dedup.

Manager-approval routing and the Verify stage create a real audit need: for any drafted or
withheld reply we must be able to say exactly what data it was based on and why it was routed
the way it was.
"""

from __future__ import annotations

from typing import Any

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import Outcome

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id TEXT PRIMARY KEY,
    thread_id  TEXT,
    category   TEXT,
    risk       TEXT,
    signals    TEXT,
    action     TEXT NOT NULL,
    error      TEXT,
    timestamp  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS routing_decisions (
    message_id   TEXT NOT NULL,
    decision     TEXT NOT NULL,
    reason       TEXT NOT NULL,
    triggered_by TEXT NOT NULL,
    timestamp    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shopify_lookups (
    message_id     TEXT NOT NULL,
    order_id       TEXT,
    fields_fetched TEXT,
    fetched_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    message_id           TEXT NOT NULL,
    draft_text           TEXT,
    verify_layer1_result TEXT,
    verify_layer2_result TEXT,
    lint_result          TEXT,
    label_applied        TEXT,
    created_at           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sent_replies (
    message_id      TEXT PRIMARY KEY,
    final_sent_text TEXT,
    edit_distance   REAL,
    sent_at         TEXT
);
CREATE TABLE IF NOT EXISTS inbox_messages (
    message_id  TEXT PRIMARY KEY,
    thread_id   TEXT,
    sender      TEXT,
    to_addr     TEXT,
    subject     TEXT,
    body        TEXT,
    received_at TEXT,
    was_unread  INTEGER,
    direction   TEXT NOT NULL DEFAULT 'inbound',
    folder      TEXT,
    imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inbox_thread ON inbox_messages(thread_id, received_at);
CREATE TABLE IF NOT EXISTS manager_approvals (
    message_id  TEXT PRIMARY KEY,
    approved_by TEXT,
    decision    TEXT,
    approved_at TEXT
);
"""


# ---------------------------------------------------------------------------- SLA KPIs
#
# First response time and resolution time are DERIVED, not stored. Every input already
# lives in inbox_messages (both directions, timestamped, threaded by Gmail's own
# X-GM-THRID), so writing them into a column would create a second copy that goes stale
# the moment a late reply is imported. Definitions live in knowledge/KPIS.md.
#
# received_at is ISO-8601 UTC (inbox_import._iso), which julianday() parses directly.
# Rows with an empty received_at — the synthetic ones written by Store.record() — are
# excluded rather than counted as zero.
THREAD_KPI_CTE = """
WITH t AS (
    SELECT thread_id,
           MIN(CASE WHEN direction='inbound'  THEN received_at END) AS first_in,
           MAX(CASE WHEN direction='inbound'  THEN received_at END) AS last_in,
           MAX(CASE WHEN direction='outbound' THEN received_at END) AS last_out,
           COUNT(*)                                                 AS msgs
    FROM inbox_messages
    WHERE received_at IS NOT NULL AND received_at != ''
    GROUP BY thread_id
),
k AS (
    SELECT t.*,
           -- The first reply that came AFTER her first message. An outbound sitting
           -- earlier in the thread is us having started the conversation, not a response.
           (SELECT MIN(m.received_at) FROM inbox_messages m
             WHERE m.thread_id = t.thread_id
               AND m.direction = 'outbound'
               AND m.received_at > t.first_in)                      AS first_reply_at,
           (SELECT m.subject FROM inbox_messages m
             WHERE m.thread_id = t.thread_id AND m.direction = 'inbound'
             ORDER BY m.received_at LIMIT 1)                        AS subject,
           (SELECT m.sender FROM inbox_messages m
             WHERE m.thread_id = t.thread_id AND m.direction = 'inbound'
             ORDER BY m.received_at LIMIT 1)                        AS sender,
           -- The latest classification for the thread, so KPI rollups can scope to
           -- customer mail. NULL until the pipeline has processed something in it.
           (SELECT p.category FROM processed_messages p
             WHERE p.thread_id = t.thread_id
             ORDER BY p.timestamp DESC LIMIT 1)                     AS category
    FROM t
    WHERE t.first_in IS NOT NULL
),
kpi AS (
    SELECT thread_id,
           subject,
           sender,
           category,
           msgs                                       AS thread_messages,
           first_in                                   AS opened_at,
           first_reply_at,
           ROUND((julianday(first_reply_at) - julianday(first_in)) * 24.0, 2)
                                                      AS first_response_hours,
           -- Resolved = our reply is the last word in the thread. If she wrote back after
           -- it, the thread is open again and the clock is still running.
           CASE WHEN last_out IS NOT NULL AND last_out > last_in
                THEN ROUND((julianday(last_out) - julianday(first_in)) * 24.0, 2) END
                                                      AS resolution_hours,
           CASE WHEN last_out IS NOT NULL AND last_out > last_in THEN 'Resolved'
                WHEN first_reply_at IS NULL           THEN 'Awaiting first reply'
                ELSE 'Open' END                       AS thread_status,
           -- How long the newest unanswered message has been sitting. Null once resolved.
           CASE WHEN last_out IS NULL OR last_out < last_in
                THEN ROUND((julianday('now') - julianday(last_in)) * 24.0, 2) END
                                                      AS open_hours,
           -- Raw ingredients for the semantic closure pass in Python: the customer's final
           -- message (is it a closing thanks?) and the answer's timestamp + elapsed hours.
           (SELECT m.body FROM inbox_messages m
             WHERE m.thread_id = k.thread_id AND m.direction = 'inbound'
             ORDER BY m.received_at DESC LIMIT 1)     AS last_inbound_body,
           last_out                                   AS last_reply_at,
           ROUND((julianday(last_out) - julianday(first_in)) * 24.0, 2)
                                                      AS answer_elapsed_hours
    FROM k
)
"""


# A closing acknowledgment: her last message is thanks/confirmation, not a new ask.
# Conservative on purpose — gratitude PLUS brevity PLUS no question PLUS no complaint.
# "Thanks, but it still hasn't arrived" must never close a thread.
GRATITUDE_RE = re.compile(
    r"\b(?:thank(?:s| you)|perfect|amazing|wonderful|great|got it|all set|sounds good|"
    r"received (?:it|them)|appreciate|works for me|that works)\b",
    re.IGNORECASE,
)
REOPENER_RE = re.compile(
    # A question mark, a complaint, or a request keeps the thread open. Bare interrogatives
    # (when/where/why/how) are deliberately NOT here: real closing notes use them
    # narratively ("someday, when we are in a different place, I will return") and an
    # actual question carries its own question mark.
    # "problem"/"issue" only count when they aren't negated: "that's no problem, thank
    # you!" is a real customer sign-off (found live) and must close, not reopen.
    r"\?|\b(?:but|however|still|hasn'?t|haven'?t|didn'?t|hadn'?t|waiting"
    r"|(?<!no )(?:issue|problem)|"
    r"wrong|missing|yet to|not (?:yet|received|arrived)|can you|could you|"
    r"would you|please (?:send|share|confirm|advise)|another question)\b",
    re.IGNORECASE,
)


def is_closing_ack(body: str | None) -> bool:
    """True when a customer message closes the conversation rather than continuing it.

    The length guard exists so an essay with a thank-you buried in it doesn't close a
    thread — but 240 was too tight for a heartfelt goodbye (a real 330-character "this
    exception made a giant difference, I am deeply grateful" got drafted against). 500
    still rejects anything letter-length.
    """
    text = " ".join((body or "").split())
    if not text or len(text) > 500:
        return False
    return bool(GRATITUDE_RE.search(text)) and not REOPENER_RE.search(text)


# An outbound reply that promises retrieval and defers the answer. Deliberately narrow:
# active-retrieval phrases only. "I'll let you know when it's back in stock" is a promise
# contingent on an outside event — following it up now would be wrong, so it doesn't match.
OPEN_COMMITMENT_RE = re.compile(
    r"\b(?:get|come)(?: right)? back to you"
    r"|\blet me (?:check|pull|confirm|find out|look into)"
    r"|\bI(?:'ll| will) (?:check|pull|confirm|find out|look into|follow up)"
    r"|\bwe(?:'ll| will) (?:check|pull|confirm|find out|look into|follow up)",
    re.IGNORECASE,
)


def _kpi_block(subset: list[dict]) -> dict:
    """Every KPI number for one set of conversations. Blank, never zero, when there is
    nothing to measure. The business-hours split is by when the conversation ARRIVED
    (Mon–Fri 9–7 ET, per the team 2026-09-11): the same first-response clock, cut into
    "she wrote during the day" and "she wrote at night or on the weekend"."""
    from .retail_calendar import in_business_hours

    def frt_of(rs):
        return [r["first_response_hours"] for r in rs if r["first_response_hours"] is not None]

    def summarise(vals):
        return {"count": len(vals),
                "avg_h": round(sum(vals) / len(vals), 2) if vals else None,
                "median_h": _r2(_median(vals)), "p90_h": _r2(_pct(vals, 0.9))}

    frt = frt_of(subset)
    ttr = [r["resolution_hours"] for r in subset if r["resolution_hours"] is not None]
    waiting = [r["open_hours"] for r in subset if r["thread_status"] == "Awaiting first reply"]
    opened = sorted(r["opened_at"][:10] for r in subset if r.get("opened_at"))
    during = [r for r in subset if r.get("opened_at")
              and in_business_hours(datetime.fromisoformat(r["opened_at"]))]
    outside = [r for r in subset if r.get("opened_at")
               and not in_business_hours(datetime.fromisoformat(r["opened_at"]))]
    return {
        "window_start": opened[0] if opened else None,
        "window_end": opened[-1] if opened else None,
        "threads": len(subset),
        "answered": len(frt),
        "pct_answered": round(100 * len(frt) / len(subset)) if subset else None,
        "pct_resolved": round(100 * len(ttr) / len(subset)) if subset else None,
        "resolved": len(ttr),
        "resolved_confirmed": sum(1 for r in subset if r["thread_status"] == "Resolved — confirmed"),
        "awaiting_first_reply": len(waiting),
        "first_response_avg_h": round(sum(frt) / len(frt), 2) if frt else None,
        "first_response_median_h": _r2(_median(frt)),
        "first_response_p90_h": _r2(_pct(frt, 0.9)),
        "resolution_avg_h": round(sum(ttr) / len(ttr), 2) if ttr else None,
        "resolution_median_h": _r2(_median(ttr)),
        "resolution_p90_h": _r2(_pct(ttr, 0.9)),
        "oldest_unanswered_h": _r2(max(waiting)) if waiting else None,
        "received_business_hours": len(during),
        "received_off_hours": len(outside),
        "frt_business_hours": summarise(frt_of(during)),
        "frt_off_hours": summarise(frt_of(outside)),
    }


def _r2(v):
    return round(v, 2) if v is not None else None


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _pct(xs: list[float], p: float) -> float | None:
    """Nearest-rank percentile. Small n, so no interpolation games."""
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, round(p * len(s) + 0.5) - 1))]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            # Additive migration for databases created before threading landed.
            cols = {r["name"] for r in c.execute("PRAGMA table_info(inbox_messages)")}
            for col, decl in (("direction", "TEXT NOT NULL DEFAULT 'inbound'"), ("folder", "TEXT")):
                if col not in cols:
                    c.execute(f"ALTER TABLE inbox_messages ADD COLUMN {col} {decl}")
            # Data hygiene, idempotent: copies of our own replies that landed in INBOX were
            # tagged inbound by the folder rule and polluted the queue and the KPI clocks.
            # The From header is authoritative. (Import now tags correctly; this fixes rows
            # stored before it did.)
            c.execute(
                "UPDATE inbox_messages SET direction='outbound' "
                "WHERE direction='inbound' AND LOWER(sender) LIKE '%aflalo@aflalonyc.com%'"
            )
            # Bodies stored before the flattened-newline fix still carry quoted history
            # ("…thank you. Jen  On Aug 26, 2026, at 9:22 AM, AFLALO wrote: …"), which
            # defeats the thank-you detector and floods the review view. Re-strip in
            # place; idempotent because a clean body has no marker to cut at.
            from .inbox_import import _strip_quoted

            dirty = c.execute(
                "SELECT message_id, body FROM inbox_messages WHERE body LIKE '%wrote:%'"
            ).fetchall()
            for row in dirty:
                cleaned = _strip_quoted(row["body"] or "")
                if cleaned != (row["body"] or ""):
                    c.execute("UPDATE inbox_messages SET body=? WHERE message_id=?",
                              (cleaned, row["message_id"]))

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def already_processed(self, message_id: str) -> bool:
        """Dedup independent of the Gmail label filter, so a partial failure (draft created
        but label write failed) can't produce a duplicate draft on retry."""
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        return row is not None

    def record(self, outcome: Outcome, thread_id: str = "", email=None) -> None:
        ts = _now()
        with self._conn() as c:
            if email is not None:
                # Upsert the message itself. INSERT OR IGNORE so a real read-only import is
                # never overwritten by a later reprocess.
                c.execute(
                    "INSERT OR IGNORE INTO inbox_messages (message_id, thread_id, sender, "
                    "to_addr, subject, body, received_at, was_unread, imported_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (email.message_id, email.thread_id, email.sender, "", email.subject,
                     email.body, "", 0, ts),
                )
            c.execute(
                "INSERT OR REPLACE INTO processed_messages "
                "(message_id, thread_id, category, risk, signals, action, error, timestamp) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    outcome.message_id,
                    thread_id,
                    outcome.category,
                    outcome.risk,
                    json.dumps(outcome.signals),
                    outcome.decision.value,
                    outcome.error,
                    ts,
                ),
            )
            c.execute(
                "INSERT INTO routing_decisions "
                "(message_id, decision, reason, triggered_by, timestamp) VALUES (?,?,?,?,?)",
                (outcome.message_id, outcome.decision.value, outcome.reason, outcome.triggered_by, ts),
            )
            if outcome.order_facts:
                c.execute(
                    "INSERT INTO shopify_lookups (message_id, order_id, fields_fetched, fetched_at) "
                    "VALUES (?,?,?,?)",
                    (
                        outcome.message_id,
                        str(outcome.order_facts.get("order_number", "")),
                        json.dumps(outcome.order_facts),
                        ts,
                    ),
                )
            if outcome.draft_text is not None or outcome.verify is not None:
                v = outcome.verify
                c.execute(
                    "INSERT INTO drafts (message_id, draft_text, verify_layer1_result, "
                    "verify_layer2_result, lint_result, label_applied, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        outcome.message_id,
                        outcome.draft_text,
                        json.dumps(v.layer1_violations if v else []),
                        json.dumps(v.layer2_violations if v else []),
                        json.dumps(outcome.lint_violations),
                        outcome.label,
                        ts,
                    ),
                )

    def stats(self) -> dict[str, int]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT action, COUNT(*) n FROM processed_messages GROUP BY action"
            ).fetchall()
        return {r["action"]: r["n"] for r in rows}

    def escalation_reasons(self) -> list[tuple[str, str, int]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT triggered_by, reason, COUNT(*) n FROM routing_decisions "
                "WHERE decision != 'draft' GROUP BY triggered_by, reason ORDER BY n DESC"
            ).fetchall()
        return [(r["triggered_by"], r["reason"], r["n"]) for r in rows]

    def coverage_gaps(self) -> list[tuple[str, int]]:
        """What keeps landing in `other` is exactly what to add as the next category."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT category, COUNT(*) n FROM processed_messages "
                "WHERE category IS NOT NULL AND action != 'draft' "
                "GROUP BY category ORDER BY n DESC"
            ).fetchall()
        return [(r["category"], r["n"]) for r in rows]


    # ------------------------------------------------------------ read-only inbox copy

    def save_inbox(self, messages) -> int:
        """Store the read-only import. Idempotent on message_id."""
        ts = _now()
        with self._conn() as c:
            for m in messages:
                c.execute(
                    "INSERT OR IGNORE INTO inbox_messages (message_id, thread_id, sender, "
                    "to_addr, subject, body, received_at, was_unread, direction, folder, "
                    "imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (m.message_id, m.thread_id, m.sender, m.to_addr, m.subject, m.body,
                     m.received_at, int(m.is_unread), m.direction, m.folder, ts),
                )
        return len(messages)

    def inbox(self, limit: int = 200) -> list:
        """The messages that actually need a reply — by either definition of "unresolved".

        1. The newest inbound with nothing outbound after it: she is waiting on us.
        2. The follow-up-owed case: WE replied last, but that reply promised to come back
           to her ("let me pull the measurements…") and nothing outbound ever followed.
           Timestamps call that thread answered; the customer is still waiting. These are
           returned with `followup_owed=True` so drafting picks up from the promise.

        One per conversation either way — drafting per-message instead of per-thread is the
        fastest way to produce noise.
        """
        from .models import Email

        with self._conn() as c:
            rows = c.execute("""
                SELECT m.message_id, m.thread_id, m.sender, m.subject, m.body
                FROM inbox_messages m
                JOIN (
                    SELECT thread_id,
                           MAX(CASE WHEN direction='inbound'  THEN received_at END) AS last_in,
                           MAX(CASE WHEN direction='outbound' THEN received_at END) AS last_out
                    FROM inbox_messages GROUP BY thread_id
                ) t ON t.thread_id = m.thread_id
                WHERE m.direction = 'inbound'
                  AND m.received_at = t.last_in
                  AND (t.last_out IS NULL OR t.last_out < t.last_in)
                ORDER BY m.received_at DESC
                LIMIT ?
            """, (limit,)).fetchall()
            # A pure thank-you closes the conversation — drafting a reply to it would be
            # noise, and counting it as 'awaiting' corrupts the backlog number.
            awaiting = [
                Email(**dict(r))
                for r in rows
                if not is_closing_ack(r["body"])
                and "aflalo@aflalonyc.com" not in (r["sender"] or "").lower()
            ]

            # Case 2: our reply was the last word, but it contained an open commitment.
            rows = c.execute("""
                SELECT m.message_id, m.thread_id, m.sender, m.subject, m.body,
                       (SELECT o.body FROM inbox_messages o
                         WHERE o.thread_id = m.thread_id AND o.direction = 'outbound'
                         ORDER BY o.received_at DESC LIMIT 1) AS final_reply
                FROM inbox_messages m
                JOIN (
                    SELECT thread_id,
                           MAX(CASE WHEN direction='inbound'  THEN received_at END) AS last_in,
                           MAX(CASE WHEN direction='outbound' THEN received_at END) AS last_out
                    FROM inbox_messages GROUP BY thread_id
                ) t ON t.thread_id = m.thread_id
                WHERE m.direction = 'inbound'
                  AND m.received_at = t.last_in
                  AND t.last_out >= t.last_in
                ORDER BY m.received_at DESC
                LIMIT ?
            """, (limit,)).fetchall()
        owed = [
            Email(
                message_id=r["message_id"], thread_id=r["thread_id"], sender=r["sender"],
                subject=r["subject"], body=r["body"], followup_owed=True,
            )
            for r in rows
            if OPEN_COMMITMENT_RE.search(r["final_reply"] or "")
        ]
        return (awaiting + owed)[:limit]

    def thread_history(self, thread_id: str, before_message_id: str) -> list[dict]:
        """Everything already said in this conversation, oldest first.

        The drafter needs this or it will repeat an answer we already gave, contradict a
        commitment someone made, or re-explain a policy the deck says to explain once.
        """
        with self._conn() as c:
            rows = c.execute("""
                SELECT direction, sender, subject, body, received_at
                FROM inbox_messages
                WHERE thread_id = ? AND message_id != ?
                ORDER BY received_at
            """, (thread_id, before_message_id)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ SLA KPIs

    def thread_kpis(self, limit: int = 500) -> list[dict]:
        """One row per conversation: how fast we answered and how long it stayed open.

        Newest-opened first. `first_response_hours` is null while nobody has replied yet;
        `resolution_hours` is null while the thread is still open. Both are deliberately
        null rather than zero — an unanswered thread has no response time, and averaging a
        zero into it would report the opposite of the truth.
        """
        with self._conn() as c:
            rows = c.execute(
                THREAD_KPI_CTE + "SELECT * FROM kpi ORDER BY opened_at DESC LIMIT ?", (limit,)
            ).fetchall()

        out = []
        for r in rows:
            k = dict(r)
            body = k.pop("last_inbound_body", None)
            last_reply_at = k.pop("last_reply_at", None)
            elapsed = k.pop("answer_elapsed_hours", None)
            if k["thread_status"] == "Resolved":
                # Our reply is the last word and nothing came back — answered, unconfirmed.
                k["thread_status"] = "Resolved — answered"
                k["resolved_at"] = last_reply_at
            elif k["thread_status"] in ("Open", "Awaiting first reply") and is_closing_ack(body):
                # Her closing thanks CLOSES the thread, confirmed by the customer — even
                # when our answer isn't in the store (a reply sent from a personal mailbox
                # leaves no Sent copy here, but her 'thank you, this made a difference'
                # proves one happened). Resolution stamps at our reply when we hold it;
                # timing stays blank when we don't, rather than invented.
                k["thread_status"] = "Resolved — confirmed"
                k["resolution_hours"] = elapsed
                k["resolved_at"] = last_reply_at
                k["open_hours"] = None
            else:
                k["resolved_at"] = None
            out.append(k)
        return out

    def kpi_summary(self) -> dict:
        """Median and p90 for both KPIs, plus the backlog they hide.

        Median leads because response times are long-tailed — one holiday-weekend thread
        drags a mean somewhere no customer actually experienced. p90 is the one that maps
        to "how bad does this get", which is the number worth setting an SLA against.

        `customers` is the SLA block over every customer conversation; `weeks` is the same
        block per 4-5-4 fiscal week of conversation START (the team's calendar, 2026-09-11),
        so each week is a fixed cohort that stops changing once its threads close.
        """
        from . import knowledge  # local import: knowledge never imports store, no cycle
        from .retail_calendar import fiscal_week

        rows = self.thread_kpis(limit=100000)
        customers = [r for r in rows if knowledge.is_customer_facing(r.get("category"))]

        cohorts: dict[str, tuple[Any, list[dict]]] = {}
        for r in customers:
            if not r.get("opened_at"):
                continue
            fw = fiscal_week(datetime.fromisoformat(r["opened_at"]).date())
            cohorts.setdefault(fw.key, (fw, []))[1].append(r)

        weeks = []
        for key in sorted(cohorts):
            fw, subset = cohorts[key]
            block = _kpi_block(subset)
            block.update({"key": key, "label": fw.label, "week_start": fw.week_start.isoformat(),
                          "week_end": fw.week_end.isoformat(),
                          "maturing": block["resolved"] < block["threads"]})
            weeks.append(block)

        # The overall block includes vendor/recruiting mail and is kept because "how fast
        # does anyone answer anything" is still a real number (`aflalo-cs report` prints it).
        out = _kpi_block(rows)
        out["customers"] = _kpi_block(customers)
        out["weeks"] = weeks
        return out

    def voice_corpus(self, limit: int = 200) -> list[dict]:
        """Real sent replies — the gold-standard voice reference from design doc §3.3."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT subject, body, received_at FROM inbox_messages "
                "WHERE direction='outbound' AND LENGTH(body) > 40 "
                "ORDER BY received_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def review_rows(self) -> list[dict]:
        """One flat row per message — the shape the reviewer actually needs, and exactly
        what gets pushed to Airtable."""
        with self._conn() as c:
            rows = c.execute(THREAD_KPI_CTE + """
                SELECT p.message_id, i.received_at, i.sender, i.subject, i.body,
                       p.category, p.risk, p.signals, p.action,
                       r.reason, d.draft_text, d.label_applied,
                       sh.order_id, sh.fields_fetched, s.final_sent_text, s.sent_at,
                       COALESCE(NULLIF(i.thread_id, ''), p.thread_id) AS thread_id,
                       kpi.first_response_hours, kpi.resolution_hours,
                       kpi.thread_status, kpi.thread_messages
                FROM processed_messages p
                LEFT JOIN inbox_messages i USING(message_id)
                LEFT JOIN routing_decisions r
                       ON r.rowid = (SELECT MAX(rowid) FROM routing_decisions
                                     WHERE message_id = p.message_id)
                LEFT JOIN drafts d
                       ON d.rowid = (SELECT MAX(rowid) FROM drafts
                                     WHERE message_id = p.message_id)
                LEFT JOIN shopify_lookups sh
                       ON sh.rowid = (SELECT MAX(rowid) FROM shopify_lookups
                                      WHERE message_id = p.message_id)
                LEFT JOIN sent_replies s USING(message_id)
                LEFT JOIN kpi ON kpi.thread_id = COALESCE(NULLIF(i.thread_id, ''), p.thread_id)
                ORDER BY p.timestamp DESC
            """).fetchall()
        return [dict(r) for r in rows]
