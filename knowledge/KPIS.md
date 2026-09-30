# Two CS KPIs: first response time and resolution time

Both are measured from the mail itself. No tagging, no macro, no discipline required from
whoever is answering — the timestamps and the inbound/outbound direction are already in
`inbox_messages`, so the numbers are a query, not a new workflow.

They are **derived, never stored**. Writing them into a column would create a second copy
that goes stale the moment a late reply is imported, and the whole point is that a thread's
numbers change as the thread continues.

---

## Definitions

The unit is a **conversation**, not a message. Gmail's own `X-GM-THRID` supplies the
identity (see `DATA_MODEL.md`), so a thread survives subject-line edits and the mail clients
that rewrite `References`.

### First response time (FRT)

> Her first message in the thread → our first reply *after* it.

```
    she wrote          we replied
  ──────●────────────────●──────────►
        └──── FRT ───────┘
```

- **The clock starts at her first message, not at the thread's first message.** When we
  started the conversation — a back-in-stock note, a shipping update — the outbound sitting
  in front of her message is not a response to anything. Counting from it produces a
  negative FRT, which is exactly the sort of number that quietly poisons an average.
- **Only the first handoff counts.** Later replies in the same thread are not first
  responses. If we care about the pace of the whole back-and-forth, that is a different
  metric and it isn't this one.
- **Unanswered → blank, not zero.** A thread nobody has replied to has no response time.
  A zero would report the opposite of the truth and drag the median toward "instant". The
  count that should move instead is `awaiting_first_reply`, and `open_hours` says how long
  the oldest one has been sitting.

### Resolution time (TTR)

> Her first message → our last reply, **counted only once our reply is the last word.**

```
  she wrote     we replied     she wrote back      we replied
 ─────●────────────●───────────────●──────────────────●────────►
      └───────────────────── TTR ───────────────────── ┘
      (blank until this point — the thread reopened in the middle)
```

- **Reopening restarts nothing and finishes nothing.** If she wrote back after our reply,
  the thread is open again, TTR stays blank, and the clock keeps running from her *first*
  message. That is deliberate: her experience of "how long did this take" runs from when
  she first wrote, not from the last round trip.
- There is no explicit "closed" state in email and we are not going to invent one. "Our
  reply was the last word" is the only closure signal an inbox actually gives you. It
  over-reports resolution slightly — a thread she simply abandoned reads as resolved — and
  that is the honest tradeoff for not asking CS to tag anything.

### Thread Status

| Status | Meaning | FRT | TTR |
| --- | --- | --- | --- |
| `Awaiting first reply` | nobody has answered | blank | blank |
| `Open` | answered, then she wrote back | set | blank |
| `Resolved` | our reply is the last message | set | set |

---

## Where the numbers live

| Layer | What |
| --- | --- |
| `store.THREAD_KPI_CTE` | the SQL. One CTE, reused by both readers below. |
| `Store.thread_kpis()` | one dict per conversation — the raw rows. |
| `Store.kpi_summary()` | median + p90 for both KPIs, plus the backlog counts. |
| `aflalo-cs report` | prints the summary under the escalation and coverage sections. |
| Airtable **`CS Threads`** | one row per conversation. **This is what SLA rollups point at.** |
| Airtable `CS Drafts` | the same KPIs ride along on each email row, for context while replying. |

**Roll up on `CS Threads`, not on `CS Drafts`.** `CS Drafts` has a row per message, so a
three-message conversation would contribute its response time three times. `CS Threads` has
exactly one row per conversation, keyed on Thread ID, and re-pushes update in place.

**Rollups scope to customer mail.** `CS Threads` carries `Category` and an `Is CS`
checkbox (false for recruiting, vendor pitches, invoices, newsletters, press). A 20-minute
reply to a sales pitch is not customer service — `kpi_summary()` reports the customer block
first and the all-mail block second, and any Airtable view should filter `Is CS`.

Median leads, not mean. Response times are long-tailed — one holiday-weekend thread drags a
mean somewhere no customer actually experienced. p90 is the number worth setting an SLA
against, because it answers "how bad does this get".

### Views worth building in `CS Threads`

- **Ageing queue** — filter `Thread Status = Awaiting first reply`, sort `Open (hrs)` desc.
  This is the only view that needs looking at daily.
- **SLA breaches** — filter `First Response (hrs) > 24` (or whatever the target lands at).
- **Where the FAQ is failing** — sort `Messages` desc. A conversation that took six turns is
  either a hard case or a template that didn't answer the question the first time.
- **Trend** — group `Opened` by week, average `First Response (hrs)`.

---

## What distorts these numbers

Read this before quoting a figure to anyone.

1. **The import window truncates old threads.** `inbox_import --days 30` means a
   conversation that started 40 days ago reports its FRT from the first message *inside the
   window*, which is usually a mid-thread reply. Ignore threads whose `Opened` sits within
   a day or two of the window's start, or import enough history that it doesn't matter.
2. **Calendar hours, not business hours.** A Friday-6pm email answered at Monday 9am reads
   as 63 hours. That is the customer's real wait, and it's the right default. A
   business-hours variant needs a support-hours calendar and a holiday list, neither of
   which exists yet — worth adding only once a target is actually set.
3. **A reply sent from someone's personal address is invisible.** Direction comes from which
   folder the message was in: `INBOX` → inbound, the shared mailbox's Sent → outbound. A
   reply that never lands in the shared Sent folder reads as "never answered".
4. **Auto-replies would count as a response** if any were sent. The import's `NOISE_SENDERS`
   filter drops the obvious machine senders on the inbound side; if a vacation autoresponder
   is ever turned on for this mailbox, it will need excluding too, or FRT collapses to
   minutes and means nothing.
5. **Threads Gmail merged** — Gmail groups by subject in some clients. Two unrelated
   questions under one subject line become one conversation with one response time.
6. **Empty `received_at` rows are excluded**, not counted as zero. Synthetic rows written by
   `Store.record()` during a mock run have no timestamp and correctly contribute nothing.

## What these two KPIs do not tell you

They measure **speed**, and speed is the easy half. A one-line "we'll look into it" is an
excellent first response time and a bad reply. The quality counterpart is `sent_replies` —
edit distance between what was drafted and what actually went out — which stays empty until
CS starts sending from the queue. Read the two together or the drafting pipeline will look
like a success for making everyone faster at sending worse email.
