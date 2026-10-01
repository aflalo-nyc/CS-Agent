"""Airtable sync — one flat row per customer email.

SQLite stays the source of truth: it's free, local, version-controllable, and the pipeline
depends on it for dedup. Airtable is a *view* on top, pushed one-way, so the team gets the
surface they already use without the pipeline gaining a network dependency it can fail on.

Uses the Airtable REST API with a personal access token. That is deliberate — the Claude
Code Airtable plugin is an MCP server for interactive exploration in a session; the running
pipeline needs its own credential and cannot rely on an MCP connection existing.

    export AIRTABLE_TOKEN=pat...
    export AIRTABLE_BASE=app...
    python -m aflalo_cs.airtable --schema     # print the tables to create
    python -m aflalo_cs.airtable --push       # sync the local store up

Two tables. `CS Drafts` is one row per email — the queue CS works from. `CS Threads` is one
row per conversation and carries the two SLA KPIs, because first response time and
resolution time are properties of a thread; averaging them over a per-message table counts
a long thread once per message.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

TABLE = "CS Drafts"
API = "https://api.airtable.com/v0"

# The reviewer-facing schema. Deliberately flat and short — one row is one customer email
# and everything a reviewer needs to act is visible without opening anything.
SCHEMA: list[dict[str, Any]] = [
    {"name": "Message ID", "type": "singleLineText", "note": "Gmail Message-ID. The unique key — dedup happens on this."},
    {"name": "Received", "type": "dateTime", "note": "When she wrote in. Sort by this."},
    {"name": "From", "type": "email", "note": "Customer address."},
    {"name": "Subject", "type": "singleLineText", "note": ""},
    {"name": "Her Email", "type": "multilineText", "note": "What she actually wrote, quoted history stripped."},
    {"name": "Status", "type": "singleSelect", "note": "The queue. This is the column you work from.",
     "options": ["Ready to send", "Needs approval", "Draft unverified", "No draft", "Possible phishing", "Thread resolved"]},
    {"name": "Priority", "type": "singleSelect",
     "note": "Urgent = high risk, anger, a named deadline, a delivery dispute, or suspected phishing. Sort the queue by this first — nothing urgent should be skimmed past.",
     "options": ["Urgent", "Normal"]},
    {"name": "Draft", "type": "multilineText", "note": "The proposed reply. Empty when Status = No draft."},
    {"name": "Why", "type": "multilineText", "note": "One line explaining the Status. Every escalation traces to a named rule."},
    {"name": "Category", "type": "singleSelect", "note": "Which CS Guide scenario matched.", "options": []},
    {"name": "Risk", "type": "singleSelect", "note": "", "options": ["low", "medium", "high"]},
    {"name": "Flags", "type": "multipleSelects", "note": "Signals the classifier set — why it escalated.", "options": []},
    {"name": "Order #", "type": "singleLineText", "note": "Extracted from her email, if present."},
    {"name": "Info Available", "type": "singleSelect",
     "note": "Could the agent get the facts this email needed — from Shopify or the info bank? 'No' is a data gap to close (missing spec, missing catalog data), and the draft carries ______ blanks for a human to fill.",
     "options": ["Yes", "No", "n/a"]},
    {"name": "Info Source", "type": "singleLineText",
     "note": "Exactly where the facts came from (Shopify order / catalog / price with currency+country / sizing specs / policy) — or what is MISSING."},
    {"name": "Thread ID", "type": "singleLineText", "note": "Gmail conversation id. Group by this to see the whole exchange, not one message."},
    {"name": "Thread Status", "type": "singleSelect", "note": "Awaiting first reply = the FRT clock is still running. Resolved = our reply was the last word.",
     "options": ["Awaiting first reply", "Open", "Resolved"]},
    {"name": "First Response (hrs)", "type": "number", "precision": 2,
     "note": "Her first message -> our first reply after it. Empty means nobody has answered yet — that is a backlog item, not a zero."},
    {"name": "Resolution (hrs)", "type": "number", "precision": 2,
     "note": "Her first message -> our last reply, counted only once the thread is closed. Empty while it is still open."},
    {"name": "Thread Msgs", "type": "number", "precision": 0, "note": "Turns in the conversation. High counts are where the FAQ is failing."},
    {"name": "Comments + Feedback", "type": "richText",
     "note": "Leave review notes here. `aflalo-cs learn` distills them into binding draft lessons — feedback in this column changes agent behavior without a code change."},
    {"name": "Sent", "type": "checkbox", "note": "Tick when you send it. Drives the quality metric."},
    {"name": "Sent At", "type": "dateTime", "note": "Auto-filled or manual."},
    {"name": "Final Sent Text", "type": "multilineText", "note": "Paste what you actually sent if you edited it. The diff against Draft is how we measure whether drafts are getting better."},
    {"name": "Open in Gmail", "type": "url",
     "note": "One click: puts this Draft into the Gmail thread as a normal draft, then opens that thread. Empty when there is no draft. Clicking twice does not make two drafts."},
]

# One row per CONVERSATION. The KPIs are properties of a thread, not of a message, and
# CS Drafts has a row per message — so averaging First Response there would count a
# three-message thread three times. This table is what any SLA rollup should point at.
THREADS_TABLE = "CS Threads"

THREAD_SCHEMA: list[dict[str, Any]] = [
    {"name": "Thread ID", "type": "singleLineText", "note": "Gmail X-GM-THRID. The unique key — re-push updates rather than duplicating."},
    {"name": "Subject", "type": "singleLineText", "note": "Subject of her first message in the thread."},
    {"name": "From", "type": "email", "note": "Customer address."},
    {"name": "Category", "type": "singleSelect", "note": "Latest classification for the thread. Empty until the pipeline has processed it.", "options": []},
    {"name": "Is CS", "type": "checkbox", "note": "Real customer mail. SLA rollups filter on this — recruiting/vendor/invoice threads answer fast and mean nothing."},
    {"name": "Thread Status", "type": "singleSelect", "note": "Resolved — confirmed: she closed it out (thanks/got it). Resolved — answered: our reply was the last word. Open: she wrote back with more. Awaiting first reply: nobody has answered.",
     "options": ["Awaiting first reply", "Open", "Resolved — answered", "Resolved — confirmed"]},
    {"name": "Opened", "type": "dateTime", "note": "Her first message. Both clocks start here."},
    {"name": "First Response At", "type": "dateTime", "note": "Date + time of our first reply to her first message."},
    {"name": "Resolved At", "type": "dateTime", "note": "Date + time of the answer that closed the thread. Empty while open."},
    {"name": "First Response (hrs)", "type": "number", "precision": 2,
     "note": "Opened -> First Reply At, calendar hours. Empty means unanswered — a backlog item, not a zero."},
    {"name": "Resolution (hrs)", "type": "number", "precision": 2,
     "note": "Opened -> our last reply, only once the thread is closed. Empty while it is still open."},
    {"name": "Open (hrs)", "type": "number", "precision": 2,
     "note": "How long the newest unanswered message has been sitting. This is the ageing queue."},
    {"name": "Messages", "type": "number", "precision": 0, "note": "Turns in the conversation. High counts are where the FAQ is failing."},
]


def _thread_row_from(r: dict) -> dict[str, Any]:
    from . import knowledge

    return {
        "Thread ID": r["thread_id"],
        "Subject": (r.get("subject") or "")[:250],
        "From": (r.get("sender") or "")[:250],
        "Category": r.get("category"),
        "Is CS": knowledge.is_customer_facing(r.get("category")),
        "Thread Status": r.get("thread_status"),
        "Opened": r.get("opened_at") or None,
        "First Response At": r.get("first_reply_at") or None,
        "Resolved At": r.get("resolved_at") or None,
        "First Response (hrs)": r.get("first_response_hours"),
        "Resolution (hrs)": r.get("resolution_hours"),
        "Open (hrs)": r.get("open_hours"),
        "Messages": r.get("thread_messages"),
    }


# Categories whose answer is standing policy — the info bank, not an API.
POLICY_GROUNDED = {
    "general_policy_question", "final_sale_return", "custom_jewelry_request",
    "returns_how_to", "return_window_question",
}


def _is_closing_ack_body(r: dict) -> bool:
    # Fossil guard: a row processed in an earlier run for a message that is itself a
    # closing thanks. Whatever the old outcome was, her message ended the conversation.
    from .store import is_closing_ack

    return is_closing_ack(r.get("body"))


URGENT_SIGNALS = {"angry_or_threatening", "time_sensitive_deadline",
                  "delivery_dispute", "possible_phishing_or_scam"}


def _priority(r: dict) -> str:
    signals = json.loads(r.get("signals") or "{}")
    if r.get("risk") == "high" or r.get("category") == "suspected_phishing_or_scam" or any(
        signals.get(k) for k in URGENT_SIGNALS
    ):
        return "Urgent"
    return "Normal"


def _provenance(r: dict) -> dict[str, str]:
    """The accountability columns: did the agent HAVE the facts this email needed, and
    where did they come from? 'No' rows are the data-gap worklist — each one is a spec to
    request or catalog data to fix, and its draft carries ______ blanks."""
    from . import knowledge

    reason = r.get("reason") or ""
    draft = r.get("draft_text") or ""
    facts = json.loads(r.get("fields_fetched") or "{}")

    missing = []
    if "___" in draft:
        missing.append("fact blanked in draft")
    if "no spec on file" in reason:
        missing.append("garment measurements not in spec folder")
    if "price lookup failed" in reason:
        missing.append("no non-zero price in Shopify")
    if missing:
        return {"Info Available": "No", "Info Source": "MISSING: " + "; ".join(missing)}

    sources = []
    if any(k in facts for k in ("fulfillment_status", "tracking_number", "estimated_delivery", "order_number")):
        sources.append("Shopify order")
    if "garment_measurements" in facts:
        sources.append("sizing specs (Drive)")
    if "sizes_in_stock" in facts or "product_description" in facts:
        sources.append("Shopify catalog")
    if "price_min" in facts:
        cur = facts.get("price_currency", "?")
        where = facts.get("price_country", "US base")
        sources.append(f"Shopify price ({cur}, {where})")
    if sources:
        return {"Info Available": "Yes", "Info Source": ", ".join(sources)}

    if r.get("category") in POLICY_GROUNDED:
        return {"Info Available": "Yes", "Info Source": "policy / info bank"}
    if not knowledge.is_customer_facing(r.get("category")):
        return {"Info Available": "n/a", "Info Source": "business mail — no facts needed"}
    return {"Info Available": "n/a", "Info Source": ""}


# One snapshot row per push: the "on average we respond within N hours" numbers, stored.
KPI_TABLE = "CS KPI Summary"

KPI_SCHEMA: list[dict[str, Any]] = [
    {"name": "Period", "type": "singleLineText", "note": "The unique key. A fiscal week ('FY2026 W34 · AUG-D (week of 8/16)' = conversations STARTED that week), 'All time' (the totals), or 'Right now' (the backlog)."},
    {"name": "Fiscal Week", "type": "singleLineText", "note": "4-5-4 retail calendar label, e.g. AUG-D. Blank on the All time / Right now rows."},
    {"name": "Period Start", "type": "dateTime", "note": "Sunday the fiscal week starts; start of the window on All time; the moment on Right now."},
    {"name": "Covers", "type": "singleLineText", "note": "The period of measurement, in words, so the row answers the question itself."},
    {"name": "Scope", "type": "singleSelect", "note": "Always 'customers': real customer mail only, vendors/recruiting excluded. This IS the SLA.",
     "options": ["customers"]},
    {"name": "Conversations", "type": "number", "precision": 0, "note": "Customer conversations, total."},
    {"name": "Answered", "type": "number", "precision": 0, "note": "Got at least a first response (so far)."},
    {"name": "% Answered", "type": "number", "precision": 0, "note": ""},
    {"name": "Resolved", "type": "number", "precision": 0, "note": "Closed, either kind."},
    {"name": "% Resolved", "type": "number", "precision": 0, "note": ""},
    {"name": "Confirmed by customer", "type": "number", "precision": 0, "note": "She closed it out herself (thanks / got it)."},
    {"name": "Median First Response (hrs)", "type": "number", "precision": 2, "note": "Half the conversations were answered faster than this."},
    {"name": "p90 First Response (hrs)", "type": "number", "precision": 2, "note": "How bad it gets — the number to set an SLA against."},
    {"name": "Avg First Response (hrs)", "type": "number", "precision": 2, "note": "Long-tailed; trust the median."},
    {"name": "Median Resolution (hrs)", "type": "number", "precision": 2, "note": ""},
    {"name": "p90 Resolution (hrs)", "type": "number", "precision": 2, "note": ""},
    {"name": "Avg Resolution (hrs)", "type": "number", "precision": 2, "note": ""},
    {"name": "Received in Business Hours", "type": "number", "precision": 0, "note": "Conversations that arrived Mon–Fri 9am–7pm ET."},
    {"name": "Median First Response, Business Hours (hrs)", "type": "number", "precision": 2, "note": "First response time for conversations that arrived during business hours."},
    {"name": "Avg First Response, Business Hours (hrs)", "type": "number", "precision": 2, "note": ""},
    {"name": "Received Off Hours", "type": "number", "precision": 0, "note": "Arrived evenings, nights, or weekends (ET)."},
    {"name": "Median First Response, Off Hours (hrs)", "type": "number", "precision": 2, "note": "First response time for conversations that arrived outside business hours."},
    {"name": "Avg First Response, Off Hours (hrs)", "type": "number", "precision": 2, "note": ""},
    {"name": "Still maturing", "type": "checkbox", "note": "Some of this week's conversations are still open, so its numbers will keep moving until they close. Finished weeks never change."},
    {"name": "Awaiting first reply", "type": "number", "precision": 0, "note": "Right-now row only: conversations with no reply yet."},
    {"name": "Oldest Unanswered (days)", "type": "number", "precision": 1, "note": "Right-now row only."},
]


def _kpi_rows(summary: dict, when: str) -> list[dict[str, Any]]:
    # One row per 4-5-4 fiscal week of conversation STARTS (a fixed cohort: finished weeks
    # never change), then the All-time totals, then the live backlog. Every row NAMES its
    # period in words (Sarena, 2026-09-03), and the weekly rows came back at the team's
    # request with their own calendar labels (2026-09-11).
    def measures(c: dict) -> dict[str, Any]:
        bh, off = c["frt_business_hours"], c["frt_off_hours"]
        return {
            "Conversations": c["threads"],
            "Answered": c["answered"],
            "% Answered": c["pct_answered"],
            "Resolved": c["resolved"],
            "% Resolved": c["pct_resolved"],
            "Confirmed by customer": c["resolved_confirmed"],
            "Median First Response (hrs)": c["first_response_median_h"],
            "p90 First Response (hrs)": c["first_response_p90_h"],
            "Avg First Response (hrs)": c["first_response_avg_h"],
            "Median Resolution (hrs)": c["resolution_median_h"],
            "p90 Resolution (hrs)": c["resolution_p90_h"],
            "Avg Resolution (hrs)": c["resolution_avg_h"],
            "Received in Business Hours": c["received_business_hours"],
            "Median First Response, Business Hours (hrs)": bh["median_h"],
            "Avg First Response, Business Hours (hrs)": bh["avg_h"],
            "Received Off Hours": c["received_off_hours"],
            "Median First Response, Off Hours (hrs)": off["median_h"],
            "Avg First Response, Off Hours (hrs)": off["avg_h"],
        }

    rows: list[dict[str, Any]] = []
    for w in summary.get("weeks", []):
        ws = w["week_start"]
        rows.append({
            "Period": w["key"],
            "Fiscal Week": w["label"],
            "Period Start": ws,
            "Covers": f"conversations started {w['label']}, {ws} to {w['week_end']}",
            "Scope": "customers",
            "Still maturing": w["maturing"],
            **measures(w),
        })
    c = summary["customers"]
    rows.append({
        "Period": "All time",
        "Period Start": c["window_start"],
        "Covers": f"every customer conversation, {c['window_start']} to {c['window_end']}",
        "Scope": "customers",
        "Still maturing": c["resolved"] < c["threads"],
        **measures(c),
    })
    rows.append({
        "Period": "Right now",
        "Period Start": when,
        "Covers": "the current backlog, this moment",
        "Scope": "customers",
        "Awaiting first reply": c["awaiting_first_reply"],
        "Oldest Unanswered (days)": round(c["oldest_unanswered_h"] / 24, 1)
        if c["oldest_unanswered_h"] else None,
    })
    return rows


STATUS_FROM_LABEL = {
    "cs/ready-to-send": "Ready to send",
    "cs/needs-approval": "Needs approval",
    "cs/draft-unverified": "Draft unverified",
    "cs/no-draft": "No draft",
    "cs/possible-phishing": "Possible phishing",
}


def print_schema() -> None:
    from . import knowledge

    for table, schema in ((TABLE, SCHEMA), (THREADS_TABLE, THREAD_SCHEMA)):
        print(f"Create a table called  {table}  with these fields:\n")
        for f in schema:
            opts = f.get("options")
            if f["name"] == "Category":
                opts = knowledge.category_names()
            if f["name"] == "Flags":
                from .models import Signals

                opts = sorted(Signals().as_dict())
            print(f"  {f['name']:<22}{f['type']:<18}" + (f"{opts}" if opts else ""))
            if f["note"]:
                print(f"  {'':<22}{'':<18}\u21b3 {f['note']}")
        print()
    print(
        "SQLite remains the source of truth. Both tables are a one-way view — if either is "
        "deleted or drifts, re-push and nothing is lost."
    )


def _row_from(r: dict) -> dict[str, Any]:
    signals = json.loads(r.get("signals") or "{}")
    return {
        "Message ID": r["message_id"],
        "Received": r.get("received_at") or None,
        "From": (r.get("sender") or "")[:250],
        "Subject": (r.get("subject") or "")[:250],
        "Her Email": (r.get("body") or "")[:95000],
        # A resolved conversation's row is done, whatever its outcome was at the time —
        # fossils ("No draft" verdicts on threads the team has since answered, thank-you
        # messages processed before closure detection) otherwise sit in the queue forever.
        "Status": (
            "Thread resolved"
            if str(r.get("thread_status") or "").startswith("Resolved")
            or _is_closing_ack_body(r)
            else STATUS_FROM_LABEL.get(r.get("label_applied") or "", "No draft")
        ),
        "Priority": _priority(r),
        "Draft": r.get("draft_text") or "",
        "Why": r.get("reason") or "",
        "Category": r.get("category") or "other",
        "Risk": r.get("risk") or "low",
        "Flags": sorted(k for k, v in signals.items() if v),
        "Order #": r.get("order_id") or "",
        **_provenance(r),
        "Thread ID": r.get("thread_id") or "",
        # Nulls are pushed as nulls on purpose. An unanswered thread has no response time,
        # and a 0 in this column would read as "answered instantly" on every rollup.
        "Thread Status": r.get("thread_status"),
        "First Response (hrs)": r.get("first_response_hours"),
        "Resolution (hrs)": r.get("resolution_hours"),
        "Thread Msgs": r.get("thread_messages"),
        "Sent": bool(r.get("sent_at")),
        "Sent At": r.get("sent_at") or None,
        "Final Sent Text": r.get("final_sent_text") or "",
    }


def _field_defs(schema: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Translate a schema into Airtable Meta-API field definitions."""
    from . import knowledge
    from .models import Signals

    choices = {
        "Status": ["Ready to send", "Needs approval", "Draft unverified", "No draft", "Possible phishing", "Thread resolved"],
        "Risk": ["low", "medium", "high"],
        "Category": knowledge.category_names(),
        "Flags": sorted(Signals().as_dict()),
        "Thread Status": ["Awaiting first reply", "Open", "Resolved — answered", "Resolved — confirmed"],
        "Priority": ["Urgent", "Normal"],
    }
    out: list[dict[str, Any]] = []
    for f in schema or SCHEMA:
        d: dict[str, Any] = {"name": f["name"], "type": f["type"]}
        if f["note"]:
            d["description"] = f["note"][:400]
        if f["type"] in ("singleSelect", "multipleSelects"):
            # The choices map covers fields whose options come from the knowledge base;
            # fields with fixed options declare them inline on the schema entry.
            opts = choices.get(f["name"], f.get("options") or [])
            d["options"] = {"choices": [{"name": c} for c in opts]}
        elif f["type"] == "dateTime":
            d["options"] = {
                "timeZone": "client",
                "dateFormat": {"name": "iso"},
                "timeFormat": {"name": "24hour"},
            }
        elif f["type"] == "date":
            d["options"] = {"dateFormat": {"name": "iso"}}
        elif f["type"] == "number":
            d["options"] = {"precision": f.get("precision", 2)}
        elif f["type"] == "checkbox":
            d["options"] = {"icon": "check", "color": "greenBright"}
        out.append(d)
    return out


class Airtable:
    def __init__(
        self,
        token: str,
        base: str,
        table: str = TABLE,
        schema: list[dict[str, Any]] | None = None,
        key_field: str = "Message ID",
        description: str = "Drafted CS replies. Written by the AFLALO CS pipeline; "
        "SQLite is the source of truth and this is a one-way view.",
    ) -> None:
        self.token, self.base, self.table = token, base, table
        self.schema = schema or SCHEMA
        self.key_field = key_field
        self.description = description

    def _meta(self, method: str, path: str, payload: dict | None = None) -> dict:
        req = urllib.request.Request(
            f"{API}/meta/bases/{self.base}/{path}" if path else f"{API}/meta/bases/{self.base}",
            data=json.dumps(payload).encode() if payload else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Airtable {exc.code}: {exc.read()[:400].decode()}") from exc

    def list_tables(self) -> list[dict]:
        return self._meta("GET", "tables").get("tables", [])

    def create_table(self) -> str:
        """Create the CS Drafts table. Idempotent — returns the id if it already exists."""
        for t in self.list_tables():
            if t["name"].lower() == self.table.lower():
                return t["id"]
        created = self._meta(
            "POST",
            "tables",
            {
                "name": self.table,
                "description": self.description,
                "fields": _field_defs(self.schema),
            },
        )
        return created["id"]

    def ensure_fields(self, table_id: str) -> list[str]:
        """Add any SCHEMA field the table is missing. Returns the names added.

        `create_table` is idempotent on the *table*, not on its columns. A base created
        before a field existed would otherwise 422 the next push, or silently drop it —
        so every push checks the columns first.
        """
        existing = {
            f["name"]
            for t in self.list_tables()
            if t["id"] == table_id
            for f in t.get("fields", [])
        }
        added = []
        for d in _field_defs(self.schema):
            if d["name"] in existing:
                continue
            self._meta("POST", f"tables/{table_id}/fields", d)
            added.append(d["name"])
        return added

    def _url(self, path: str, params: dict | None = None) -> str:
        # Encode each path segment separately: spaces in table names must become %20,
        # but the '/' between table and record id must SURVIVE (quoting it turns
        # "Pull Requests/rec123" into one nonexistent table name and Airtable 403s),
        # and the query string is built apart so '?' is never eaten either.
        quoted = "/".join(urllib.parse.quote(seg, safe="") for seg in path.split("/"))
        url = f"{API}/{self.base}/{quoted}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        return url

    def _call(
        self, method: str, path: str, payload: dict | None = None, params: dict | None = None
    ) -> dict:
        url = self._url(path, params)
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode() if payload else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Airtable {exc.code}: {exc.read()[:300].decode()}") from exc

    def existing_ids(self) -> dict[str, str]:
        """Map key field -> Airtable record id, so a re-push updates instead of duplicating."""
        out, offset = {}, None
        while True:
            params: dict = {"fields[]": self.key_field, "pageSize": 100}
            if offset:
                params["offset"] = offset
            data = self._call("GET", self.table, params=params)
            for rec in data.get("records", []):
                mid = rec.get("fields", {}).get(self.key_field)
                if mid:
                    out[mid] = rec["id"]
            offset = data.get("offset")
            if not offset:
                return out

    def purge(self) -> int:
        """Delete every record in the table. Used to clear synthetic data before real mail."""
        ids = list(self.existing_ids().values())
        for batch in (ids[i : i + 10] for i in range(0, len(ids), 10)):
            self._call("DELETE", self.table, params={"records[]": batch})
        return len(ids)

    def push(self, rows: list[dict], row_fn=_row_from) -> tuple[int, int]:
        known = self.existing_ids()
        mapped = [row_fn(r) for r in rows]
        create = [{"fields": f} for f in mapped if f[self.key_field] not in known]
        update = [
            {"id": known[f[self.key_field]], "fields": f}
            for f in mapped
            if f[self.key_field] in known
        ]
        for batch in (create[i : i + 10] for i in range(0, len(create), 10)):
            self._call("POST", self.table, {"records": batch, "typecast": True})
        for batch in (update[i : i + 10] for i in range(0, len(update), 10)):
            self._call("PATCH", self.table, {"records": batch, "typecast": True})
        return len(create), len(update)


def sync_store(token: str, base: str, store, link_for=None):
    """Push the local store up: CS Drafts, CS Threads, CS KPI Summary. Yields one summary
    line per table. `link_for(message_id) -> url` fills the Open in Gmail column for rows
    that carry a draft (the service passes it; the CLI leaves it empty)."""
    from datetime import datetime, timezone

    drafts = Airtable(token, base)
    threads = Airtable(
        token, base, THREADS_TABLE, THREAD_SCHEMA, key_field="Thread ID",
        description="One row per CS conversation, with first response and resolution time. "
        "SLA rollups belong here, not on CS Drafts — that table has a row per message.",
    )
    for at in (drafts, threads):
        tid = at.create_table()
        added = at.ensure_fields(tid)
        if added:
            yield f"table '{at.table}': added fields " + ", ".join(added)

    def row_fn(r: dict) -> dict:
        f = _row_from(r)
        f["Open in Gmail"] = link_for(r["message_id"]) if (link_for and f.get("Draft")) else None
        return f

    rows = store.review_rows()
    created, updated = drafts.push(rows, row_fn=row_fn)
    yield f"pushed {len(rows)} rows to '{TABLE}' — {created} created, {updated} updated"

    kpis = store.thread_kpis(limit=100000)
    created, updated = threads.push(kpis, row_fn=_thread_row_from)
    yield f"pushed {len(kpis)} conversations to '{THREADS_TABLE}' — {created} created, {updated} updated"

    summary_table = Airtable(
        token, base, KPI_TABLE, KPI_SCHEMA, key_field="Period",
        description="Customer-service SLA. One row per 4-5-4 fiscal week of conversation "
        "starts (AUG-D etc.), plus All time totals and the Right now backlog. First response "
        "is also split by whether the customer wrote during business hours (Mon–Fri 9–7 ET). "
        "Updated on every sync.",
    )
    tid = summary_table.create_table()
    summary_table.ensure_fields(tid)
    when = datetime.now(timezone.utc).isoformat()
    created, updated = summary_table.push(_kpi_rows(store.kpi_summary(), when), row_fn=lambda r: r)
    yield f"pushed KPI rows to '{KPI_TABLE}' — {created} created, {updated} updated"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", action="store_true", help="print the tables to create")
    ap.add_argument("--push", action="store_true", help="sync the local store up")
    ap.add_argument("--create-table", action="store_true", help="create both tables in the base")
    ap.add_argument("--list-tables", action="store_true", help="show what is already in the base")
    ap.add_argument("--purge", action="store_true", help="DELETE every record in both tables")
    args = ap.parse_args()

    if args.schema or not (args.push or args.create_table or args.list_tables or args.purge):
        print_schema()
        return 0

    # Import config BEFORE reading the environment — importing it is what loads .env.
    # Read them first and a configured .env looks exactly like no credentials at all.
    from . import config

    token, base = os.environ.get("AIRTABLE_TOKEN"), os.environ.get("AIRTABLE_BASE")
    if not (token and base):
        print(
            "Set AIRTABLE_TOKEN and AIRTABLE_BASE — in the environment or in "
            f"{config.ROOT / '.env'}."
        )
        return 2

    drafts = Airtable(token, base)
    threads = Airtable(
        token, base, THREADS_TABLE, THREAD_SCHEMA, key_field="Thread ID",
        description="One row per CS conversation, with first response and resolution time. "
        "SLA rollups belong here, not on CS Drafts — that table has a row per message.",
    )

    if args.list_tables:
        for t in drafts.list_tables():
            print(f"  {t['id']}  {t['name']:<28}{len(t.get('fields', []))} fields")
        return 0

    if args.purge:
        for at in (drafts, threads):
            try:
                print(f"deleted {at.purge()} records from '{at.table}'")
            except RuntimeError as exc:
                print(f"skipped '{at.table}': {exc}")
        if not args.push:
            return 0

    # One-time rename: "First Reply At" -> "First Response At" (clearer per review).
    # Renaming keeps the column's data; ensure_fields would otherwise create an empty twin.
    try:
        for t in threads.list_tables():
            if t["name"] == THREADS_TABLE:
                for f in t.get("fields", []):
                    if f["name"] == "First Reply At":
                        threads._meta("PATCH", f"tables/{t['id']}/fields/{f['id']}",
                                      {"name": "First Response At"})
                        print("renamed 'First Reply At' -> 'First Response At'")
    except RuntimeError:
        pass

    # Always reconcile the schema before touching records — pushing a field the table
    # doesn't have is a 422, and that is the failure mode every time a column is added.
    for at in (drafts, threads):
        tid = at.create_table()
        added = at.ensure_fields(tid)
        if args.create_table or added:
            print(f"table '{at.table}' ready — https://airtable.com/{base}/{tid}")
        if added:
            print("  added fields: " + ", ".join(added))
    if args.create_table and not args.push:
        return 0

    from .store import Store

    for line in sync_store(token, base, Store(config.DB_PATH)):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
