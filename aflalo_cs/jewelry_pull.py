"""Campaign pull: international customers who asked about jewelry.

Asked 2026-09-11 — the store enabled international pricing + checkout on jewelry, and
the team wants to tell the people who previously asked. Two groups, per the brief:

  (1) pricing_only   — asked what a piece costs and it went no further
  (2) real_interest  — after learning the price, kept going: wanted to buy, asked how
                       to pay / ship / size, confirmed a piece, chased for an answer

Method, in order of how much is trusted:
  - Candidate threads come from the real catalog: every jewelry product title in Shopify
    (the 'Accessories' type, duplicates collapsed) plus the plain words for jewelry.
    A thread is a candidate if ANY message in it mentions one.
  - The model reads each candidate thread end to end and fills a fixed schema. Country
    must be backed by quoted evidence from the thread (a signature, "ship to Canada", a
    .de address, prices we quoted in CAD); with no evidence it says unknown, never guesses.
  - The output is a worklist, not a verdict: every row carries its evidence quote so a
    human can check it in ten seconds before an email goes out.

Runs as `python -m aflalo_cs.jewelry_pull` → CSV under data/exports/ (gitignored: it is
customer PII) and the 'Jewelry Intl Inquiries' table in the CS Airtable base.
"""

from __future__ import annotations

import csv
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config  # noqa: F401  (loads .env before anything reads the environment)
from .airtable import Airtable
from .llm import LLM, AnthropicLLM, cached_system

OUR_DOMAIN = "aflalonyc.com"

GENERIC_TERMS = [
    "jewelry", "jewellery", "jewelleries", "ring", "rings", "necklace", "bracelet",
    "earring", "earrings", "pendant", "bangle", "cuff", "huggie", "stud", "diamond",
    "platinum", "white gold", "gold-plated", "toe ring",
]
NOT_JEWELRY_TITLES = re.compile(r"\b(belt|pant|scarf|bag|hat)\b", re.I)

TABLE = "Jewelry Intl Inquiries"
SCHEMA: list[dict[str, Any]] = [
    {"name": "Thread ID", "type": "singleLineText", "note": "Gmail thread id. The unique key."},
    {"name": "Customer", "type": "singleLineText", "note": "Name as signed or as in the From header."},
    {"name": "Email", "type": "email", "note": "The address to reach them at."},
    {"name": "Country", "type": "singleLineText", "note": "Only when the thread proves it; else 'unknown'."},
    {"name": "International", "type": "singleSelect", "note": "Outside the US? Unknown when the thread doesn't say.",
     "options": ["Yes", "No", "Unknown"]},
    {"name": "Group", "type": "singleSelect", "note": "(1) Pricing only: asked the price, went no further. (2) Real interest: kept going after learning the price.",
     "options": ["Pricing only", "Real interest after pricing", "Existing jewelry buyer", "Domestic", "Not a jewelry customer", "Unclear"]},
    {"name": "Pieces", "type": "singleLineText", "note": "Catalog pieces asked about."},
    {"name": "Asked Pricing", "type": "checkbox", "note": ""},
    {"name": "Received Pricing", "type": "checkbox", "note": "We told them a price in the thread."},
    {"name": "Evidence", "type": "multilineText", "note": "Short quotes from the thread that justify Country and Group. Check these before emailing."},
    {"name": "Summary", "type": "multilineText", "note": "One or two sentences on what happened."},
    {"name": "Subject", "type": "singleLineText", "note": ""},
    {"name": "First Message At", "type": "dateTime", "note": ""},
    {"name": "Last Message At", "type": "dateTime", "note": ""},
    {"name": "Messages", "type": "number", "precision": 0, "note": "Total in the thread, both directions."},
]

SYSTEM = """You read a customer-service email thread from AFLALO, a New York fashion and fine
jewelry brand, and fill a fixed record about it. Rules:

- Use only what the thread says. Every claim about the customer's country must be backed by a
  quote you copy into `evidence` — a signature line, a city, "ship to Canada", a phone country
  code, a non-US email domain (.de, .ca, .co.uk, .com.au; rogers.com is a Canadian ISP), prices
  we quoted in a non-USD currency, or duties/customs talk. If nothing in the thread indicates a
  country, set country to "unknown" and international to "unknown". Never infer a country from
  a name.
- `asked_pricing`: the customer asked what a piece costs (price, cost, how much, pricing, quote).
- `received_pricing`: AFLALO stated a price or price range in the thread.
- `interest_after_pricing`: AFTER a price was given, the customer wrote again with intent —
  wants to buy, asks how to pay or ship, confirms a size or piece, asks about timing or
  availability, negotiates, says they'll take it. A bare "thanks" is not interest. Asking for a
  price a second time is not interest.
- `group`, exactly one:
    "pricing_only"   — asked pricing, no interest_after_pricing (whether or not we answered)
    "real_interest"  — interest_after_pricing is true
    "existing_buyer" — the customer already BOUGHT a jewelry piece (an order number, an invoice
                       paid, a repair of a piece they own); the thread is after-sale
    "not_jewelry_customer" — the thread is not a customer asking about jewelry for themselves
                       (press, wholesale, a stylist pulling for a client, an order-status thread
                       that merely names a jewelry piece, an internal forward with no customer)
    "unclear"        — a customer and jewelry, but you cannot tell what they wanted
  Do NOT use the customer's country to pick the group; the group is about intent only.
- `customer_email`: the customer's address as it appears in a From header. Never an
  @aflalonyc.com address.
- Keep `evidence` to the shortest quotes that prove country and group. Keep `summary` to two
  sentences."""

SCHEMA_JSON = {
    "type": "object",
    "properties": {
        "is_jewelry_inquiry": {"type": "boolean"},
        "customer_name": {"type": "string"},
        "customer_email": {"type": "string"},
        "country": {"type": "string"},
        "international": {"type": "string", "enum": ["yes", "no", "unknown"]},
        "pieces": {"type": "array", "items": {"type": "string"}},
        "asked_pricing": {"type": "boolean"},
        "received_pricing": {"type": "boolean"},
        "interest_after_pricing": {"type": "boolean"},
        "group": {"type": "string",
                  "enum": ["pricing_only", "real_interest", "existing_buyer", "not_jewelry_customer", "unclear"]},
        "evidence": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["is_jewelry_inquiry", "customer_name", "customer_email", "country",
                 "international", "pieces", "asked_pricing", "received_pricing",
                 "interest_after_pricing", "group", "evidence", "summary"],
    "additionalProperties": False,
}

GROUP_LABEL = {
    "pricing_only": "Pricing only",
    "real_interest": "Real interest after pricing",
    "existing_buyer": "Existing jewelry buyer",
    "not_jewelry_customer": "Not a jewelry customer",
    "unclear": "Unclear",
}


def jewelry_titles(shopify) -> list[str]:
    """Live catalog: Accessories-type titles, '(International Duplicate)' twins collapsed,
    the few non-jewelry accessories (belts, scarves) dropped."""
    body = shopify._graphql(
        '{ products(first: 250, query: "product_type:Accessories") { edges { node { title } } } }'
    )
    titles = set()
    for e in body["data"]["products"]["edges"]:
        t = re.sub(r"\s*\(International Duplicate\)\s*$", "", e["node"]["title"]).strip()
        if t and not NOT_JEWELRY_TITLES.search(t):
            titles.add(t)
    return sorted(titles)


def _term_pattern(titles: list[str]) -> re.Pattern:
    # Titles match on their distinctive lead ("Hex Ring", "Bicep Bangle") so a customer who
    # writes "the hex ring" without the material still counts.
    leads = set()
    for t in titles:
        lead = re.split(r"\s+in\s+", t, maxsplit=1)[0]
        leads.add(lead)
    alts = [re.escape(x) for x in sorted(leads | set(GENERIC_TERMS), key=len, reverse=True)]
    return re.compile(r"\b(?:" + "|".join(alts) + r")\b", re.I)


def candidate_threads(db: Path, pattern: re.Pattern) -> list[dict]:
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    threads: dict[str, dict] = {}
    for r in c.execute("SELECT thread_id, sender, to_addr, subject, body, direction, received_at "
                       "FROM inbox_messages ORDER BY received_at"):
        t = threads.setdefault(r["thread_id"], {"thread_id": r["thread_id"], "subject": r["subject"] or "",
                                                "messages": [], "hit": False})
        t["messages"].append(dict(r))
        if pattern.search(f"{r['subject'] or ''}\n{r['body'] or ''}"):
            t["hit"] = True
    out = []
    for t in threads.values():
        has_customer = any(m["direction"] == "inbound" and OUR_DOMAIN not in (m["sender"] or "").lower()
                           for m in t["messages"])
        if t["hit"] and has_customer:
            out.append(t)
    return out


def _render(thread: dict, per_message: int = 2500) -> str:
    parts = [f"SUBJECT: {thread['subject']}"]
    for m in thread["messages"]:
        who = "CUSTOMER" if m["direction"] == "inbound" else "AFLALO"
        parts.append(f"[{who} | From: {m['sender']} | {m['received_at'][:16]}]\n"
                     f"{(m['body'] or '').strip()[:per_message]}")
    return "\n\n".join(parts)


def classify(llm: LLM, thread: dict) -> dict:
    return llm.structured(system=cached_system(SYSTEM), user=_render(thread),
                          schema=SCHEMA_JSON, effort="high", max_tokens=1500)


def to_row(thread: dict, verdict: dict) -> dict[str, Any]:
    intl = verdict["international"]
    group = GROUP_LABEL[verdict["group"]]
    if verdict["group"] in ("pricing_only", "real_interest", "existing_buyer") and intl == "no":
        group = "Domestic"          # a jewelry customer, just not the campaign's audience
    if not verdict["is_jewelry_inquiry"]:
        group = "Not a jewelry customer"
    times = [m["received_at"] for m in thread["messages"] if m["received_at"]]
    return {
        "Thread ID": thread["thread_id"],
        "Customer": verdict["customer_name"],
        "Email": verdict["customer_email"] if OUR_DOMAIN not in verdict["customer_email"] else "",
        "Country": verdict["country"] or "unknown",
        "International": {"yes": "Yes", "no": "No"}.get(intl, "Unknown"),
        "Group": group,
        "Pieces": ", ".join(verdict["pieces"])[:250],
        "Asked Pricing": verdict["asked_pricing"],
        "Received Pricing": verdict["received_pricing"],
        "Evidence": verdict["evidence"],
        "Summary": verdict["summary"],
        "Subject": thread["subject"][:250],
        "First Message At": min(times) if times else None,
        "Last Message At": max(times) if times else None,
        "Messages": len(thread["messages"]),
    }


def write_csv(rows: list[dict], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [f["name"] for f in SCHEMA]
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in cols})
    return path


def report(rows: list[dict]) -> str:
    def block(label: str, subset: list[dict]) -> list[str]:
        lines = [f"{label} ({len(subset)}):"]
        for r in sorted(subset, key=lambda r: (r["Country"], r["Customer"])):
            lines.append(f"  - {r['Customer']} <{r['Email']}> — {r['Country']} — {r['Pieces'] or r['Subject']}")
        return lines

    intl = [r for r in rows if r["International"] == "Yes"]
    pricing = [r for r in intl if r["Group"] == "Pricing only"]
    interest = [r for r in intl if r["Group"] == "Real interest after pricing"]
    buyers = [r for r in intl if r["Group"] == "Existing jewelry buyer"]
    unknown = [r for r in rows if r["International"] == "Unknown"
               and r["Group"] in ("Pricing only", "Real interest after pricing")]
    domestic = [r for r in rows if r["Group"] == "Domestic"]
    countries = sorted({r["Country"] for r in intl if r["Country"] not in ("", "unknown")})
    out = []
    out += block("GROUP 1 — international, asked pricing only", pricing) + [""]
    out += block("GROUP 2 — international, real interest after pricing", interest) + [""]
    out += block("Already bought jewelry from abroad (proven buyers)", buyers) + [""]
    out += block("Country unknown from the thread (check before adding)", unknown) + [""]
    out.append(f"Domestic jewelry inquiries (not in the campaign): {len(domestic)}")
    out.append(f"Countries seen among international inquiries: {', '.join(countries) or 'none proven'}")
    return "\n".join(out)


def main() -> int:
    from .config import get_shopify

    shopify, _ = get_shopify()
    titles = jewelry_titles(shopify)
    pattern = _term_pattern(titles)
    db = Path(os.environ.get("AFLALO_DB", "data/cs.db"))
    threads = candidate_threads(db, pattern)
    print(f"{len(titles)} jewelry pieces in the catalog; {len(threads)} candidate threads")

    llm = AnthropicLLM()
    rows = []
    for i, t in enumerate(threads, 1):
        try:
            rows.append(to_row(t, classify(llm, t)))
        except Exception as exc:  # one unreadable thread must not lose the rest
            print(f"  thread {t['thread_id']} skipped: {exc}")
        print(f"  {i}/{len(threads)} read", end="\r")
    print()

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = write_csv(rows, Path(f"data/exports/jewelry_intl_inquiries_{stamp}.csv"))
    print(f"CSV: {path}")

    token, base = os.environ.get("AIRTABLE_TOKEN"), os.environ.get("AIRTABLE_BASE")
    if token and base:
        table = Airtable(token, base, TABLE, SCHEMA, key_field="Thread ID",
                         description="International customers who asked about jewelry, grouped by "
                         "intent, with the thread evidence. Built for the international-pricing "
                         "launch campaign (2026-09-11). Check Evidence before emailing anyone.")
        tid = table.create_table(); table.ensure_fields(tid)
        created, updated = table.push(rows, row_fn=lambda r: r)
        print(f"Airtable '{TABLE}': {created} created, {updated} updated — https://airtable.com/{base}/{tid}")

    print()
    print(report(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
