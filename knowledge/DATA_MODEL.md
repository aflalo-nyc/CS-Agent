# Data model

Two stores, one direction of travel.

```
  aflalo@aflalonyc.com                    SQLite  (data/cs.db)              Airtable
  ────────────────────                    ──────────────────────            ────────
   read-only IMAP  ──────────────────►  inbox_messages ──── thread_kpis() ─────►  CS Threads
   (EXAMINE, never writes)                    │              (one per convo)       (one-way view)
                                              ▼                                │
                                        processed_messages ─┐                  │
                                        routing_decisions   ├── review_rows() ──►  CS Drafts
                                        shopify_lookups     │   (one flat row)     (one-way view)
                                        drafts             ─┤
                                        sent_replies       ─┘
                                        manager_approvals
```

`CS Threads` carries the two SLA KPIs and is computed from `inbox_messages` alone — it needs
nothing the pipeline produces, so it is populated the moment the inbox is imported, before a
single draft exists.

**SQLite is the source of truth.** The pipeline depends on it for dedup and it must work
offline. Airtable is a one-way view pushed on top, so a rate limit or a schema change there
can never break drafting. If the Airtable table is deleted, re-push and nothing is lost.

The schema self-heals: every table is `CREATE TABLE IF NOT EXISTS` and runs on every
`Store()` open, so adding a table to `store.py` migrates existing databases automatically.

---

## Threading — how conversations are stored

**The unit of work is a conversation, not a message.** Three decisions follow from that:

**Thread identity comes from Gmail, not from us.** The import reads `X-GM-THRID` via the
IMAP extension — Gmail's own conversation id. Reconstructing threads from
`References`/`In-Reply-To` is the usual approach and it breaks whenever a mail client
rewrites those headers, which Outlook and several mobile clients do. `References` is kept
only as a fallback when `X-GM-THRID` is absent.

**Both sides of the conversation are stored.** The import reads INBOX *and* Sent Mail, and
tags each message `direction = inbound | outbound`. This matters twice over:
- The drafter is shown the prior turns, so it cannot repeat an answer already given or
  contradict a commitment someone already made. The deck is explicit: don't explain the
  policy twice.
- Real sent replies are the gold-standard voice corpus design doc §3.3 asks for —
  `Store.voice_corpus()` returns them. No separate export needed.

**Only unanswered threads get drafted.** `Store.inbox()` returns one message per
conversation: the newest inbound, and only where nothing outbound came after it. A thread
someone already replied to is not awaiting a draft. Drafting per-message instead would
produce a draft for every message in every thread, including ones answered days ago — the
fastest possible way to make the queue useless.

```
T-A   she wrote → we replied → she wrote again     →  DRAFT (newest inbound, unanswered)
T-B   she wrote → we replied                       →  skip  (already answered)
T-C   she wrote                                    →  DRAFT
```

## 1. `inbox_messages` — the read-only copy of the real inbox

Written by `inbox_import.py`. This is the only table containing real customer mail, and it
is populated by a connection that cannot write to Gmail (IMAP `EXAMINE` + `BODY.PEEK`).

| Column | Type | Notes |
| --- | --- | --- |
| `message_id` | TEXT **PK** | RFC-822 `Message-ID`. The join key for every other table. |
| `thread_id` | TEXT | First entry of `References`, else the message's own id. |
| `sender` | TEXT | Raw `From` header. |
| `to_addr` | TEXT | Raw `To` — useful for spotting mail that wasn't actually to CS. |
| `subject` | TEXT | |
| `body` | TEXT | Plain text, quoted thread history stripped. |
| `received_at` | TEXT | Raw `Date` header. |
| `was_unread` | INTEGER | Whether it was unread **at import time**. Recorded, never changed. |
| `direction` | TEXT | `inbound` (she wrote) or `outbound` (we replied). |
| `folder` | TEXT | `INBOX` or the Sent folder it came from. |
| `imported_at` | TEXT | UTC ISO-8601. |

## 2. `processed_messages` — dedup + state

One row per message the pipeline has handled. This is the idempotency guard: it is checked
*before* anything runs, independently of Gmail labels, so a partial failure (draft written,
label write failed) can't produce a duplicate on retry.

| Column | Type | Notes |
| --- | --- | --- |
| `message_id` | TEXT **PK** | |
| `thread_id` | TEXT | |
| `category` | TEXT | One of the 26 in `categories.yaml`. |
| `risk` | TEXT | `low` / `medium` / `high`. |
| `signals` | TEXT (JSON) | The 8 classifier booleans. |
| `action` | TEXT | `draft` / `needs-manager-approval` / `draft-unverified` / `needs-human`. |
| `error` | TEXT | Set when a message failed; the run continues past it. |
| `timestamp` | TEXT | |

## 3. `routing_decisions` — why, in words

Append-only. This is what makes escalations auditable: every outcome traces to one named
rule, queryable without reading code.

| Column | Type | Notes |
| --- | --- | --- |
| `message_id` | TEXT | |
| `decision` | TEXT | |
| `reason` | TEXT | Human-readable: `"angry/threatening signal set"`, `"UNSUPPORTED CLAIM — …"`. |
| `triggered_by` | TEXT | `safety-net` / `signal` / `risk` / `category` / `lookup` / `verify` / `error`. |
| `timestamp` | TEXT | |

## 4. `shopify_lookups` — the factual-accuracy audit trail

Exactly which order facts fed each draft. Without this you cannot answer "why did it say
that?" after the fact.

| Column | Type | Notes |
| --- | --- | --- |
| `message_id` | TEXT | |
| `order_id` | TEXT | |
| `fields_fetched` | TEXT (JSON) | Only the fields that category asked for — nothing else reached the drafter. |
| `fetched_at` | TEXT | |

## 5. `drafts` — what was written and what the guardrail found

| Column | Type | Notes |
| --- | --- | --- |
| `message_id` | TEXT | |
| `draft_text` | TEXT | Kept even when a check failed — the audit trail needs the bad draft too. |
| `verify_layer1_result` | TEXT (JSON) | Deterministic scan violations. |
| `verify_layer2_result` | TEXT (JSON) | Model fact-check violations. |
| `lint_result` | TEXT (JSON) | Voice-lint violations. |
| `label_applied` | TEXT | `cs/ready-to-send` etc. |
| `created_at` | TEXT | |

## 6. `sent_replies` — the quality signal *(empty until CS starts sending)*

The only table that tells you whether this is working. `edit_distance` between `draft_text`
and `final_sent_text` is the metric in design doc §4.2, and the one that decides Fyxer vs.
custom. Nothing else generates it.

| Column | Type | Notes |
| --- | --- | --- |
| `message_id` | TEXT **PK** | |
| `final_sent_text` | TEXT | What actually went out. |
| `edit_distance` | REAL | Fraction changed before sending. |
| `sent_at` | TEXT | |

## 7. `manager_approvals` — who signed off *(empty until the approval flow runs)*

| Column | Type | Notes |
| --- | --- | --- |
| `message_id` | TEXT **PK** | |
| `approved_by` | TEXT | |
| `decision` | TEXT | |
| `approved_at` | TEXT | Also useful later for judging whether a rule is worth loosening. |

---

## The flat view

`Store.review_rows()` LEFT JOINs all of the above into one row per email. That is what a
reviewer sees and exactly what gets pushed to Airtable — nobody should have to join tables
to answer "what came in, what did we write, can I send it."

## Airtable — `CS Drafts`

| Field | Type | Source |
| --- | --- | --- |
| Message ID | singleLineText | `inbox_messages.message_id` — unique key, re-push updates rather than duplicating |
| Received | dateTime | `received_at` |
| From / Subject / Her Email | email / text / long text | `inbox_messages` |
| **Status** | singleSelect | `Ready to send` · `Needs approval` · `Draft unverified` · `No draft` |
| Draft | multilineText | `drafts.draft_text` |
| Why | multilineText | `routing_decisions.reason` |
| Category / Risk / Flags | selects | `processed_messages` |
| Order # | singleLineText | `shopify_lookups.order_id` |
| Thread ID | singleLineText | Gmail `X-GM-THRID` |
| Thread Status / First Response (hrs) / Resolution (hrs) / Thread Msgs | select + numbers | derived — see `KPIS.md` |
| **Sent** | checkbox | ticked by CS |
| **Sent At** | dateTime | |
| **Final Sent Text** | multilineText | pasted by CS if they edited |

The last three are the only fields a human fills in, and they're what closes the loop back
into `sent_replies`.

## Airtable — `CS Threads`

One row per **conversation**, keyed on Thread ID. Pushed from `Store.thread_kpis()`.

| Field | Source |
| --- | --- |
| Thread ID | `inbox_messages.thread_id` — unique key, re-push updates in place |
| Subject / From | her first message in the thread |
| Thread Status | `Awaiting first reply` · `Open` · `Resolved` |
| Opened / First Reply At | the two timestamps both clocks run between |
| First Response (hrs) | her first message → our first reply after it |
| Resolution (hrs) | her first message → our last reply, only once the thread is closed |
| Open (hrs) | how long the newest unanswered message has been sitting |
| Messages | turns in the conversation |

**SLA rollups point here, not at `CS Drafts`.** `CS Drafts` has a row per message, so a
three-message conversation would contribute its response time three times to any average.
The same KPIs appear on `CS Drafts` too, but only as context while writing a reply.

### Nothing about this is stored

Both KPIs are computed by `store.THREAD_KPI_CTE` at read time from `inbox_messages`. There
is no KPI table and no KPI column, because a stored number goes stale the moment a late
reply is imported — and a thread's numbers are supposed to change as the thread continues.
Definitions, edge cases, and what distorts them: `KPIS.md`.
