# AFLALO CS Agent — how it works, what it's fed, and what's real

*The complete explanation for the team. Everything below runs against live systems —
the real inbox, the real Shopify store, the real Airtable base. Written 2026-08-26.*

## What it is, in one paragraph

An agent that reads the shared CS inbox, understands each unresolved conversation in
context, and writes a reply in AFLALO's voice using only facts it can prove — live order
data, live catalog and inventory, live per-country prices, Production's garment specs, and
written policy. Every draft goes to a human who reads it and sends it. The agent
**structurally cannot send email** (its credential has no send permission) and **cannot
mark anything read** (the code contains no operation that can). When it doesn't have a
fact, it says so — with a `______` blank for the human to fill, never a guess.

## The pipeline, one email at a time

```
inbox (read-only copy) ──► scam net ──► classify ──► route (deterministic rules)
                                                        │
              ┌─────────────────────────────────────────┤
              ▼                                         ▼
        needs a human                        retrieve facts (Shopify, specs, policy)
        (no draft, reason logged)                       │
                                                        ▼
                                            draft (brand voice + real reply examples)
                                                        │
                                            VERIFY — 2-layer fact check
                                                        │
                                         pass → review queue   fail → withheld, reason logged
```

Two design decisions carry the whole thing:

1. **The model observes; deterministic code decides.** The classifier only describes the
   email (category, risk, signals). Plain, auditable rules decide draft-vs-escalate — so
   every escalation traces to one named rule, not a model's mood.
2. **One grounding container.** Every fact source — order data, measurements, prices,
   inventory — enters the same structure, and the verifier checks every number, date, and
   claim in the draft against it. A draft stating anything it can't prove is withheld
   automatically.

## APIs — what we read, with which permission

| System | Auth | What we read | Used for |
| --- | --- | --- | --- |
| **Shopify Admin GraphQL** | Client-credentials token, minted fresh every 24h, read-only scopes only | Orders (status, tracking, dates), catalog (sizes, per-size stock, incoming replenishment, descriptions), per-country prices, ships-to countries, customer name/order count | Order status, sizing, availability, restock, pricing answers |
| **Gmail (IMAP)** | App Password | INBOX + Sent, **read-only by protocol** (EXAMINE + BODY.PEEK — the server cannot mark anything read) | The local conversation copy the agent drafts against |
| **Anthropic API** | API key | Three model calls per email: classify → draft → fact-check | The language work |
| **Airtable REST** | Personal access token | Write-only push of three tables | The review surface |

Granted Shopify scopes (all read): `read_orders`, `read_all_orders`, `read_products`,
`read_inventory`, `read_inventory_shipments(+received_items)`, `read_customers`,
`read_returns`, `read_price_rules`, `read_custom_fulfillment_services`. There is **no
write scope of any kind** — the agent cannot modify an order, issue a refund, or touch the
store.

## How prices are calculated — the exact chain

1. The customer states her country in the email ("I'm in Italy"). The classifier extracts
   it **as she wrote it** — never inferred from her email address.
2. `"Italy"` maps to `IT` through a fixed table. An unrecognized country → no lookup; the
   draft asks. Never a guess.
3. One GraphQL call: every variant's `contextualPricing` **for that country**. Shopify
   itself returns the amount *and the currency* — we never convert. Verified live:
   Italy → EUR, UK → GBP.
4. **Every $0.00 is discarded.** The $0s are our own price-suppressed duplicate jewelry
   pages — a 0 is a sentinel, never a price. Min–max of what remains becomes the quote:
   a single price, or a range when variants differ.
5. The draft must include the market disclaimer (prices vary with the diamond and metal
   market; final price confirmed at purchase) and is **manager-gated** — a human confirms
   every quoted number before it goes out.
6. If she named no country: the USD reference price is quoted with the disclaimer, plus
   one question — her ship-to country. A real number either way.
7. Enforcement: any amount in a draft — $, €, or £ — that is not in the retrieved data is
   caught by a deterministic scan and the draft is withheld. This is adversarially tested:
   10/10 planted-fake-price attacks caught.

## What the agent is fed (the "training" — and what that word really means here)

**No model weights were trained or fine-tuned.** The agent's competence is architecture:
what it's shown, what it's forbidden, and what's checked. That is deliberate — every
behavior is inspectable and changeable in an afternoon, and nothing is locked inside a
trained black box.

| Input | Source | Role |
| --- | --- | --- |
| Brand voice rules | Emily's brand deck, transcribed | The register: warm not gushing, one line of warmth, lead with the resolution, `Warmly, Eva` |
| **120 real sent replies** | The inbox's own Sent folder | Cleaned to 90 usable exemplars; each new email gets the 3 most similar past replies as tone reference. Their facts are quarantined by construction — an exemplar's order number isn't in the retrieved data, so copying it fails verification |
| 26+ scenario playbooks | The CS Guide deck | Per-category approach + template, tiered by risk (draft / manager-gated / human-only) |
| Policy facts | Notion SOPs + deck, every fact with provenance | What a draft may state about returns, exchanges, damage, shipping |
| Garment specs (8 styles) | Production's Drive folder | Exact finished-garment measurements, quotable and verified like a tracking number |
| Thread history | The conversation itself | The drafter sees every prior turn — it continues conversations, never restarts them |

Voice is additionally enforced by a deterministic lint: required signoff, banned corporate
phrases, max one exclamation point, max one closing invitation ("one line of warmth is
enough" is a rule the machine checks, not a vibe).

## The guardrails, concretely

- **Cannot send.** No send permission exists on any credential the pipeline holds.
- **Cannot mark read.** Verified by test: no label-removal operation exists in the code.
- **Two-layer fact check on every draft.** Layer 1: a deterministic scan traces every
  order number, tracking number, date, amount, measurement, and product attribute back to
  retrieved data. Layer 2: a separate model call cross-examines the prose (e.g. "has
  shipped" written over a status of "unfulfilled"). Either failing withholds the draft.
- **A verifier that couldn't run counts as a failure**, never a pass.
- **Scam net.** Deterministic impersonation marks (built from a real gifting-scam specimen
  in this inbox): celebrity loan requests, freemail follow-ups into professional threads,
  lookalike domains, banking-detail changes. Flagged mail gets **no reply of any kind** —
  even a decline confirms a live inbox — and a quarantine label with the marks listed.
- **Money is gated.** Prices draft at manager tier. A polite refund or cancellation ask
  gets a manager-gated draft (the human decides the remedy, starting from a draft instead
  of a blank page); an ANGRY one, and anything rated high-risk, goes straight to a person
  with no draft.
- **Fill-in blanks, never deferrals.** A missing fact becomes `______` in an otherwise
  finished sentence. "Let me check and get back to you" is banned — the human reading the
  draft *is* the person who would check.

## KPIs — measured from the mail itself

No tagging or discipline required; the timestamps and directions are already in the data.

- **First response**: her first message → our first reply. Stored as a datetime and in hours.
- **Resolution**: her first message → the answer that closed the thread. A thread closes
  two ways, and the agent tells them apart:
  - **Resolved — confirmed**: her last message is a closing acknowledgment ("perfect,
    thank you!") — detected, conservatively: gratitude + short + no question + no
    complaint. "Thanks, but it still hasn't arrived" never closes anything.
  - **Resolved — answered**: our reply stands as the last word.
- Unanswered threads are **blank, never zero** — a zero would say "answered instantly".
- Rollups scope to customer mail; a fast reply to a sales pitch flatters no number.
- Stored in Airtable: per-conversation rows (`CS Threads`) and daily snapshot averages —
  avg / median / p90, in hours (`CS KPI Summary`).

## The Airtable surface (base: AFLALO, 3 tables)

| Table | Grain | What it answers |
| --- | --- | --- |
| `CS Drafts` | one row per email | The queue: her email, the draft, status, why, **Info Available** (could the agent get the facts? No = a data gap to close), **Info Source** (exactly where each fact came from, currency and country named) |
| `CS Threads` | one row per conversation | The SLA: opened, first response at, resolved at, hours, status, ageing |
| `CS KPI Summary` | one row per day per scope | "On average we respond in N hours" — stored, trendable |

## Where everything lives — the map for changing things

Every behavior traces to a file you can open and edit. No behavior is locked in a model.

| You want to change… | Edit | How it works |
| --- | --- | --- |
| A reply template, or how a scenario is handled | `knowledge/categories.yaml` | One entry per scenario: the template text, the approach guidance, the risk tier (`draft` / `manager` / `human`), which Shopify fields it needs |
| **How an email gets classified** | Same file | The classifier is shown every category's approach text as its definition and must pick one. A "vendor pitch" is whatever the `vendor_or_agency_pitch` entry describes: cold outreach for ads/growth/software/agency services. To sharpen a boundary, sharpen the words |
| The generic business-mail replies | Same file | `recruiting_or_talent`, `vendor_or_agency_pitch`, `partnership_or_wholesale`, `billing_or_invoice` each carry their skeleton template (greeting, one safe line, closing) |
| What escalates to a human | `aflalo_cs/router.py` | The plain-code rules: risk ratings, warning signals, per-category exemptions. Every escalation names its rule |
| Policy facts a draft may state | `knowledge/policy_facts.yaml` | Every fact carries its source. The return window and fee cite the **published site policy** (aflalonyc.com/policies/refund-policy, fetched 2026-08-26) |
| The voice rules | `knowledge/brand_voice.md` + `aflalo_cs/voice_lint.py` | The deck's rules as machine checks: signoff, banned phrases, one exclamation point, one closing offer, **no em dashes** |
| Garment measurements | `knowledge/sizing.yaml` | Add a spec file's numbers here and the pipeline quotes them |
| Scam detection marks | `aflalo_cs/phishing.py` | The deterministic impersonation patterns |

## The Airtable base, exactly

| Table | One row per | Key | Written by |
| --- | --- | --- | --- |
| `CS Drafts` | email needing action | Message ID | pipeline (re-push updates in place) |
| `CS Threads` | conversation | Thread ID | pipeline |
| `CS KPI Summary` | day × scope | Snapshot | pipeline |

`CS Drafts` columns: her email, Status (`Ready to send` / `Needs approval` / `Draft
unverified` / `No draft` / `Possible phishing`), the Draft, **Why** (the named rule or
violation behind the status — read this before judging a draft), Category, Risk, Flags,
Order #, **Info Available** + **Info Source** (could the agent get the facts, and from
exactly where), thread KPIs for context, and the three human-filled fields (Sent, Sent At,
Final Sent Text) that close the quality loop.

`CS Threads` columns: Opened, First Response At, Resolved At (date-times), First Response /
Resolution / Open in hours, four-way Thread Status, Category, Is CS.

Storage under the hood: a local SQLite database is the source of truth (append-only audit
trail of every decision, draft, verification result, and the exact facts each draft used).
Airtable is a one-way view pushed on top; if a table is ever deleted, re-push and nothing
is lost.

## What's real vs. what's test scaffolding

Everything user-facing is real: the inbox copy is the real mailbox (read-only), Shopify
calls hit the live store, drafts quote live prices and live stock, Airtable holds real
mail. **Nothing shown to a reviewer or customer originates from demo data.**

The `fixtures/` directory is the offline test harness — synthetic orders and products that
let 142 automated tests and an adversarial red-team suite run without touching production
or spending API calls. Config decides which is used: when credentials exist, the live
clients are chosen; fixtures exist only so the safety checks themselves can be tested.
Deleting them would delete the proof that the guardrails work. They are clearly separated
and never reachable in a live run.

## What's genuinely novel here vs. off-the-shelf drafting (e.g. Fyxer)

Honest framing: no individual technique is unpublished science. The composition is what's
distinctive, and it's the difference between "AI writes plausible emails" and "an agent
that is allowed to state only what it can prove":

1. **Observe/decide split** — the model never decides what happens to an email; auditable
   rules do. Every outcome traces to a named rule.
2. **Provable grounding** — one fact container, deterministic token-tracing, adversarially
   tested. Generic tools trust the model; this one checks it.
3. **Structural safety** — cannot-send and cannot-mark-read are properties of credentials
   and protocol, not promises in a prompt.
4. **Voice from the real corpus with fact quarantine** — it writes like Eva because it's
   shown Eva's actual replies, and it can't leak another customer's details because those
   facts fail verification by construction.
5. **Answer-first with fill-in blanks** — a missing fact produces a finished draft minus
   one number, and a provenance column that turns every gap into a to-do.
6. **Self-building worklists** — missing specs, unrecognized categories, and data gaps
   accumulate in the logs and Airtable automatically; the system tells you how to improve it.
7. **Semantic thread closure** — resolution is confirmed by the customer's own words, not
   just timestamps, and thank-you messages don't get drafted replies.

## Standing gaps (all data, not code)

1. Garment specs for **Tavi Pant** and **Sagan jeans** (the styles customers actually ask
   about) → Production. The pipeline picks them up automatically.
2. **Preorder terms** in writing — a real customer asked; no document states them.
3. ~~Two policy conflicts~~ — **resolved 2026-08-26** by the site's published refund
   policy: 14 days from ship date domestic / 7 from delivery international; $20/$50 refund
   fee, waived for store credit. Goodwill exceptions stay manager-gated.
4. Shopify scope `read_inventory_transfers` → unlocks restock **dates** (quantities
   already work).
5. Deck says "Warmly, Eva"; the team actually signs "Best, Eva" — someone should rule.

## Deploy plan (after team approval)

1. Google OAuth client for Gmail (one-time; the token's scopes exclude send — the
   structural guarantee) → drafts appear in the inbox UI, attached to threads, unread kept unread.
2. Supervised live test: 2–3 emails, watched.
3. Always-on loop (Railway or equivalent): every 10–15 min, import → draft → push
   Airtable. Idempotent by design; re-runs are safe.
4. The review loop stays: humans read-to-send everything. The `Sent / Final Sent Text`
   columns feed the draft-vs-sent edit distance — the metric that eventually decides how
   much trust the agent has earned.
