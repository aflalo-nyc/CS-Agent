# Source-of-truth conflicts found while building the knowledge base

The onboarding page names two sources of truth — Emily's CS deck and the Notion Customer
Service Guide. They disagree in five places. Three are cosmetic and I resolved them; two
change what a drafted reply says about money and eligibility, and need a human ruling.

Owner for a decision: **Sarena** (policy) / **Jordyn** (#customer-service) / **Emily** (deck).

---

## BLOCKING — pipeline routes these to a human until resolved

### 1. Does the 14-day return window run from delivery or from ship date?

| Source | Says |
| --- | --- |
| CS deck, policy slide (Aug 2026) | "Return Window — **14 days from delivery**" |
| Notion Return Policy (May 2026) | "within **14 days of the date the order was shipped**" |

Ship date and delivery date can be a week apart on a slow ground shipment. This single fact
decides whether a customer is inside the window (draftable) or outside it (manager approval
for store credit). Guessing wrong tells a customer she's out of luck when she isn't, or
promises a refund we won't honor.

**Until resolved:** `out_of_window_return` and `final_sale_return` are `needs-human`; the
drafter is not given a window figure at all.

### 2. The restocking fee is missing from the deck entirely

Notion Return Policy: **$20 domestic / $50 international**, waived for in-store returns.
The deck never mentions it, and its templates say "full refund" / "a full refund has been
issued to your original payment method."

Slack confirms the fee is real *and* discretionary — Maria Sofia, 2026-08-08: *"There is a
client we said we would wave the $20 to restock but when they sent the return she didn't get
it refunded."* So a draft saying "full refund" is sometimes right and sometimes a $20 error,
and the difference lives in someone's head.

**Until resolved:** no draft states a refund amount or the word "full refund." Cancellation
and return categories that touch refund amounts route to a human.

---

## RESOLVED — decision recorded, flagging for confirmation

### 3. Portal-first vs. label-attached

- Notion CS Guide: send her to `returns.aflalonyc.com`; **"Do not proactively offer label
  generation — let customers attempt the portal first."**
- CS deck: several templates say a return label **is attached** ("a return label is attached
  to send back the incorrect item", "I've attached a prepaid return label").

**Resolution:** both are right for different cases. Customer-initiated returns → portal
(the Notion rule). AFLALO-error returns (wrong item, damaged, cancel-in-transit) → we attach
the label, because the deck is explicit that our mistakes are not her problem to navigate.
Encoded per-category in `categories.yaml`. Note the pipeline cannot actually create a label —
it only drafts text saying one is attached, and a human attaches it (see README §Boundaries).

### 4. Greeting style

Notion CS Guide: *"Always greet warmly — open every message with 'Thank you for writing to us'
or 'We'd be happy to assist.'"* The deck says the opposite: *"Get to the point. She is not
reading a paragraph of preamble before the answer,"* and *"Lead with the resolution."*

**Resolution:** deck wins. It is newer, it is the brand team's artifact, and the onboarding
page names it the source of truth. The Notion guide's greeting rule reads as pre-rebrand.

### 5. The deck contradicts itself on the sign-off

Every template and the DO list say `Warmly, Eva`. "Wrong Item Received" says `Thank you, Eva`.

**Resolution:** treated as a typo; `Warmly, Eva` is enforced everywhere by the voice lint.
Worth one line of confirmation from Emily.

---

## RESOLVED 2026-08-26 — conflicts #1 and #2, by the published site policy

`aflalonyc.com/policies/refund-policy` — the policy every customer agrees to at checkout —
states: domestic returns **within 14 days of the ship date** (portal); international
**within 7 days of delivery** (email); refunds carry a **$20 fee ($50 international),
waived entirely for store credit**; refunds land within 10 business days of receipt.

That settles #1 (window runs from SHIP date domestically, deck's "from delivery" slide
superseded) and #2 (the fee is real, published, and store credit is the no-fee path). It
matches the Notion Return Policy and the team's own sent mail ("our 14-day return window").
The drafter now receives these as stateable facts with the site as provenance. Goodwill
exceptions (waiving the fee, out-of-window credit) remain manager-gated — the site
publishes the rule, not the exceptions.

## Gaps — real questions with no documented answer

These came out of #customer-service, where Maria Sofia is currently answering CS by hand.
Each one is a category the taxonomy can't cover yet, so each routes to `needs-human` and
shows up in the "what to add next" signal described in the README.

| Question (source) | Status |
| --- | --- |
| Jewelry resizing — "Order 6415 got the Racquet String bracelet for his wife but it's too big, can we resize?" (2026-08-10) | No documented resizing service. Alteration Policy covers garments only. |
| Welcome / first-time-buyer code | **Answered in Slack** — Ava Murray: "no we dont have first time customer codes !!" Encoded in `policy_facts.yaml`; still not in any SOP. |
| Klarna refunds on unfulfilled orders (2026-08-10) | Resolved ad hoc by Lillian. No SOP. |
| Waiving the $20 restocking fee | Happens, undocumented, see #2 above. |

## 6. The signoff: the deck says "Warmly, Eva", the team writes "Best, Eva"

Found 2026-08-23 while turning the real sent replies into voice exemplars: the corpus signs
"Best," / "Best regards," almost uniformly, while the deck's hard constraint (and our voice
lint) mandates "Warmly, Eva". The deck is the designated source of truth so lint still
enforces "Warmly" — but every human edit that flips it back to "Best" will show up in the
draft-vs-sent diff. Whoever owns the deck should rule: update the deck, or tell the team.
