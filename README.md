# AFLALO — CS inbox drafting pipeline (Phase 1)

**Current state and what's left: [STATUS.md](STATUS.md)**

Takes the shared customer service inbox from "a person writes every reply" to "most replies
are drafted in Aflalo's voice, a human reads-to-send or lightly edits."

**It never sends.** The Gmail credential is scoped to read / label / draft only — no send
scope — so a bug in the pipeline logic structurally cannot email a customer.

```text
Gmail ──poll──► dedup ──► [hard safety net] ──► classifier ──► ROUTER (deterministic)
                                                                  │
             ┌────────────────────────────────────────────────────┤
             ▼                                                    ▼
      needs-human / needs-manager-approval              Shopify lookup (read-only)
      (label, stop, still logged)                              │
                                                               ▼
                                              retrieve template ──► drafter ──► VERIFY
                                                                                  │
                                              ┌───────────────────────────────────┤
                                              ▼                                   ▼
                                      violation → withhold                  Gmail draft
                                      (needs-human)                         + label + log
```

## Status (2026-10-02): deployed, waiting on one value

Everything is built, tested (173 tests), and running on Railway as the `agent` service in the
`aflalo-cs-agent` project. Drafts go to Airtable only; the Open in Gmail link places a draft
into the Gmail thread on demand. **One thing is left to complete:**

1. Create a Gmail **App Password** for aflalo@aflalonyc.com at
   https://myaccount.google.com/apppasswords (16 letters, shown once; the account's normal
   password is refused because 2-step verification is on).
2. Set it on Railway: `agent` service → Variables → `AFLALO_IMAP_PASSWORD`. The service
   redeploys itself and the first cycle runs within a minute.
3. Check the deploy log for `agent: imported … messages (read-only)` and the Airtable
   `CS Drafts` table for the new `Open in Gmail` column.

Until then every cycle logs `AFLALO_IMAP_PASSWORD not set — cycle skipped` and nothing happens.
Full handoff: `docs/AGENT.html`.

## The project page

`docs/AGENT.html` is the one-page explanation: using the Airtable queue, the system map, how
the brain decides, what it knows, the KPIs, the Railway service, and handoff. Also published at
https://claude.ai/artifact/ST8X4AJgMxbmV7wYEFsujt

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m aflalo_cs.cli doctor        # what's wired up right now
.venv/bin/python -m aflalo_cs.cli doctor --probe  # ...and whether the APIs actually answer
.venv/bin/python -m pytest tests/ -q            # 65 deterministic tests, no API key needed
.venv/bin/python -m eval.redteam --layer1-only  # adversarial grounding pass, no API key
.venv/bin/python -m eval.dryrun                 # full pipeline over the 18-email mock inbox
```

With `ANTHROPIC_API_KEY` set, the real thing:

```bash
.venv/bin/python -m aflalo_cs.cli run --show-drafts   # mock inbox, real model
.venv/bin/python -m eval.redteam                      # both guardrail layers
.venv/bin/python -m aflalo_cs.cli run --live-inbox    # real Gmail, still draft-only
.venv/bin/python -m aflalo_cs.cli report              # audit trail + coverage gaps
```

## What's real vs. stubbed

| Integration | Status |
| --- | --- |
| **Anthropic API** | Real. `claude-opus-5`, schema-constrained structured output on all three calls, prompt caching on the stable system prefix, refusal/timeout handling. Needs `ANTHROPIC_API_KEY`. |
| **Gmail** | Real `google-api-python-client` client (`GmailMailbox`), OAuth scopes pinned to read/label/draft. `MockMailbox` runs the same code path against `fixtures/mock_inbox.json`. |
| **Shopify** | **Live.** Real Admin GraphQL client (`ShopifyAdminClient`) behind the same `Shopify` protocol as `FixtureShopify`. Authenticates with the Dev Dashboard client id + secret via the client credentials grant — a 24h token, minted on demand and re-minted before expiry. App `sanskriti-cs` on `aflalo.myshopify.com`, 6 read-only scopes. Verified against real orders. |
| **Knowledge base** | Real. Extracted from Emily's CS deck, the Notion Ecommerce SOPs, and #customer-service. |
| **Slack / Fyxer** | Not integrated — connectors aren't authorized. |

## Shopify credentials

Shopify hands out three different secrets and only one of them goes in an API request.
This trips everyone up once:

| Credential | Looks like | What it's for |
| --- | --- | --- |
| Client ID | 32 hex chars | identifies the app. Not a credential you call with. |
| Client secret | `shpss_…` | signs OAuth exchanges and webhooks. Not a credential you call with. |
| **Access token** | `shpat_…` | **the `X-Shopify-Access-Token` header. This is the one.** |

There are two ways to end up holding an access token, and `config.get_shopify()` supports
both — set whichever pair you have and it picks:

- **Dev Dashboard app** → set `SHOPIFY_API_KEY` + `SHOPIFY_API_SECRET` + `SHOPIFY_SHOP_DOMAIN`.
  The client id and secret are exchanged for a token at run time via the **client credentials
  grant**, cached, and re-minted a minute before the 24-hour expiry. No merchant OAuth click
  and no token to paste anywhere. Requires the app and the store to be in the same Shopify
  organization.
- **Admin-created custom app** → set `SHOPIFY_ADMIN_TOKEN` + `SHOPIFY_SHOP_DOMAIN`. The
  `shpat_` token is long-lived and revealed exactly once, at *Apps → Develop apps → your app
  → API credentials*. If set, it wins and nothing is exchanged.

Scopes are granted **to an installation, not to an app config**. Editing scopes and
releasing a version changes what the app asks for; the existing install keeps its old grant
until it is reinstalled. And if two similarly-named apps exist on the store, it is entirely
possible to configure one and authenticate as the other — which is why `doctor --probe`
prints the app title it actually authenticated as.

`.env` is loaded automatically by `config.load_dotenv()` — no dependency, and a real
environment variable always beats the file. `aflalo-cs doctor` says which path is active and,
if neither is, exactly which value is missing.

## Knowledge base

`knowledge/` is the "learn the voice and FAQs" deliverable, and it's the part that stays
useful regardless of whether we build or buy — Fyxer needs the same inputs.

- **`brand_voice.md`** — tone, the DO/DON'T list, and the hard constraints, from the deck.
- **`policy_facts.yaml`** — every fact a draft may state, each with provenance.
- **`categories.yaml`** — 26 categories with the deck's own template language, which Shopify
  fields each needs, its tier, and its gate.
- **`COVERAGE.md`** — every deck slide → category → tier → what a human does before sending.
- **`KPIS.md`** — first response time and resolution time: how they're defined, where they
  land in Airtable, and the six things that distort them.
- **`JEWELRY_PRICING.md`** — why international customers email for prices, and how to read a
  real price out of Shopify past the $0 duplicate pages.
- **`SIZING.md`** + **`sizing.yaml`** — finished-garment measurements for 8 styles from
  Production's spec folder, three withheld with the reason. The drafter may quote a
  measurement and may never recommend a size.
- **`CONFLICTS.md`** — **read this one.** The two designated sources of truth disagree in five
  places; two of them change what a reply says about money.

### The two blocking conflicts

1. **The return window runs from delivery (deck) or from ship date (Notion Return Policy).**
   These can be a week apart and they decide eligibility.
2. **The $20/$50 restocking fee exists in Notion and is absent from the deck**, whose
   templates promise a "full refund." Slack shows it's real *and* sometimes waived by hand.

Until someone rules, every category touching either fact routes to a human and the drafter
isn't given the numbers at all — `test_policy_digest_withholds_the_unresolved_facts` enforces that.

## Scenario coverage

**All 19 inbound scenarios in the CS Guide deck produce a draft** — see `knowledge/COVERAGE.md`
for the slide-by-slide table. 26 categories: 10 draft, 10 manager, 6 human. The 6 human-tier
categories are not deck scenarios; they're gaps with no documented policy (exchanges,
discount codes, resizing, messenger delivery, jewelry pricing, and `other`).

Three tiers, by risk, not by category popularity:

- **draft** — informational, no money and no policy exception.
- **needs-manager-approval** — drafted, but gated. Every expedited request and every
  out-of-window store credit, regardless of how confident the model is. Policy fact, not a
  confidence-calibration problem.
- **needs-human** — the safety net, any escalating signal, high risk, a failed lookup, or a
  verify violation. No draft produced.

## Guardrails

**Classify and decide are separate.** The model observes — category, risk, and eight boolean
signals. Deterministic code in `router.py` turns those into the decision, so every escalation
traces to one named rule and is testable independent of model behaviour.

**An impersonation/scam net runs before the model.** Built against a real specimen (the
"Katy Perry premiere" gifting-loan request, chased from a freemail address): deterministic,
explainable marks — product-out request, celebrity event, deadline pressure, freemail
follow-up in a professional thread, lookalike domains, banking-detail changes. Flagged mail
gets **no draft of any kind** (even a decline confirms a live inbox), the `cs/possible-phishing`
label, and a reason that names each mark plus the verification step. It never renders a
verdict — real stylist pulls look exactly like fake ones, which is the whole con. Measured
on 239 real inbound messages: 3 flagged, 0 customer emails among them.

**Grounding is two layers.** Layer 1 is a deterministic scan for the kinds of tokens that are
dangerous to invent (order/tracking numbers, dates, amounts, product attributes, discount
codes, carrier facilities, weekday promises) plus a status/claim cross-check. Layer 2 is a
narrow model call that fact-checks prose. Either failing withholds the draft entirely.

Layer 1 alone currently catches **10/10** adversarial attacks with no false positive on the
control draft (`eval/redteam.py`), so grounding does not depend on a model call succeeding.

**A verifier that couldn't run is not a pass** — an API error in layer 2 is a violation.

## What the mock run shows

18 seeded emails, several drawn from real #customer-service threads:
6 auto-drafted, 2 manager-gated, 10 human — 8 drafts written in total. The escalations are the interesting part — legal language caught
before the model ran, an order number that doesn't exist caught at lookup, a non-English
message, a resize request with no policy, a welcome-code request (we don't have one), and
the Italy jewelry price question.

## Known boundaries

- **The pipeline cannot attach a return label or create one.** Categories whose template says
  "a label is attached" are drafted at manager tier at most; a human attaches the file.
- **Expedited shipping can't be fully drafted** even at manager tier — the post-upgrade
  arrival date doesn't exist until someone buys the upgrade, so the drafter writes around it
  and flags for review.
- **Carrier-scan detail** ("held up at their facility in X") comes from `carrier_location`,
  which Shopify may not populate depending on how shipping is integrated. Worth checking
  against real data before assuming a 4th API (UPS/AfterShip) is needed.
- **Jewelry price inquiries are un-draftable today** — only the Italy sheet is built, it lives
  at a claude.ai artifact URL we can't send to customers, and its shipping input is still a
  $75 placeholder. That's a content blocker, not a pipeline one — see `knowledge/JEWELRY_PRICING.md`.
- **The two SLA KPIs are calendar hours, not business hours.** A Friday-evening email answered
  Monday morning reads as 63 hours, which is the customer's real wait. A business-hours
  variant needs a support-hours calendar that doesn't exist yet.

## Testing

`tests/` are mocked-model unit tests — routing, both grounding layers, voice lint,
idempotency under a failed label write, API-failure handling, the two SLA KPIs (including
the cases that would silently report a zero or a negative), and knowledge-base integrity
(no draftable template may contain a price, fee, refund, or discount code). Fast and free.

`eval/redteam.py` is a separate adversarial pass on the guardrail. `eval/dryrun.py` runs the
whole pipeline with a rule-based stand-in for the model.

Draft *quality* and voice-match need the real model and a human scorer — that's the benchmark
in the design doc §4, and it needs the held-out set of real past tickets we don't have yet.

## Next

See [STATUS.md](STATUS.md).
