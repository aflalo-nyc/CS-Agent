# Scenario coverage — every slide in the CS Guide deck

**All 19 inbound scenarios in the deck produce a draft.** None is refused for being hard.
The two outbound scenarios are excluded per design doc §2.0 (they're triggered by inventory
changing, not by an email arriving).

Tiers follow design doc §2.9. Tier 3 is a *runtime* outcome — reached via the safety net, an
escalating signal, high risk, a failed lookup, or a verify violation — never a property of
the scenario itself.

| Deck slide | Category | Tier | Gate | What the human does before it sends |
| --- | --- | --- | --- | --- |
| Where Is My Order | `where_is_my_order` | 1 draft | — | Read and send |
| Preorder — When Will It Ship | `preorder_ship_timing` | 1 draft | — | Read and send |
| Order Status / Shipping Timeline | `order_status_shipping_timeline` | 1 draft | — | Read and send |
| Lost/Delayed 1 — Delayed by UPS | `delayed_package_carrier` | 1 draft | — | Read and send |
| Final Sale Return | `final_sale_return` | 1 draft | — | Read and send |
| Damaged Item | `damaged_item` | 1 draft | — | Read and send (it only asks for a photo) |
| Pre-Purchase Fit Question | `fit_question` | 1 draft | — | Read and send (gather-details half only — see below) |
| Out of Stock 2 — Not Restocking | `out_of_stock_discontinued` | 1 draft | — | Read and send |
| Custom Jewelry Request | `custom_jewelry_request` | 1 draft | — | Read and send |
| — (general policy questions) | `general_policy_question` | 1 draft | — | Read and send |
| Out-of-Window Return | `out_of_window_return` | 2 manager | policy | Approve the store-credit exception |
| Expedited Shipping 1 — Repeat | `expedited_shipping_repeat` | 2 manager | policy | Approve, then add the new arrival date |
| Expedited Shipping 2 — New | `expedited_shipping_new` | 2 manager | policy | Approve, then add the quoted cost |
| Wrong Item Received | `wrong_item_received` | 2 manager | action | Book the overnight reship, attach the label |
| Lost/Delayed 2 — Delayed by AFLALO | `delayed_package_aflalo` | 2 manager | action | Book the overnight, add the new tracking |
| Lost/Delayed 3 — Lost in Transit | `lost_in_transit` | 2 manager | action | Initiate the replacement, add new tracking |
| Pre Order Cancel 1 — Not Yet Shipped | `preorder_cancel_not_shipped` | 2 manager | action | Process the refund in Shopify first |
| Pre Order Cancel 2 — Already In Transit | `preorder_cancel_in_transit` | 2 manager | action | Attach the prepaid return label |
| Cancelled Item / Unable to Fulfill | `cancelled_unable_to_fulfill` | 2 manager | action | Process the refund first |
| Out of Stock 1 — Restocking / Waitlist | `out_of_stock_restocking` | 2 manager | action | Add her to the waitlist first |
| Back in Stock — Personal Outreach | *excluded* | — | — | Outbound; separate workflow (§2.0) |
| — (waitlist follow-up) | *excluded* | — | — | Outbound; separate workflow (§2.0) |

**10 draft · 10 manager · 6 human.** The 6 human-tier categories are not deck scenarios —
they're gaps found in Notion and #customer-service with no documented policy to draft from:
`exchange_request`, `discount_code_request`, `alteration_or_resize_request`,
`messenger_delivery_request`, `jewelry_price_inquiry` (parked), `other`.

## The two gate types

`policy` — the deck says get approval *before committing*. Approval is a judgment call an
approver makes.

`action` — the deck's template asserts something already done ("I've already initiated a
replacement", "a full refund has been issued", "I've added you to the waitlist", "a return
label is attached"). The deck itself insists on this: *"the email says 'has been issued', so
make it true"* and *"Add her to the waitlist yourself before replying."* The pipeline is
read-only by design (§2.6), so a human performs the action and then sends. The draft is
still fully written — that's the time saving.

> Worth a decision from Sarena: these are two genuinely different review queues, and they
> currently share one Gmail label. A fourth label (`needs-action`) would let an approver sort
> "decide this" from "do this then send." I kept the three labels the design doc specifies.

## Where a draft deliberately stops short of the deck's template

In each case the missing piece is a fact no read-only system can hold. The sentence is
dropped rather than guessed, and `flag_for_review` is set.

| Scenario | Omitted | Why |
| --- | --- | --- |
| Expedited (repeat) | New arrival date | Doesn't exist until the upgrade is bought. The stored `estimated_delivery` is the *pre*-upgrade estimate — grounded but wrong-meaning. |
| Expedited (new) | The cost | No rate card and no quoting API. |
| Delayed by AFLALO / Lost in transit | New tracking number, new date | Don't exist until a human books the shipment. |
| Pre-order cancel, Cancelled item | Refund amount, business-day count | Restocking fee unresolved — CONFLICTS.md #2. |
| Out-of-window, Cancel-in-transit | Return-window length | Delivery-vs-ship-date unresolved — CONFLICTS.md #1. |
| Out of stock (restocking) | Restock timeframe | No inventory forecast. `read_inventory` may partly unblock this. |
| Out of stock (not restocking) | Two named alternatives with links | No product-similarity retrieval. `read_products` would unblock. |
| Fit question | The size recommendation | Needs garment measurements. The deck itself says *"If you genuinely don't know a specific measurement, check with the team before guessing."* `read_products` would unblock. |
| Delayed by UPS | Facility name, when not retrieved | Falls back to the deck's own alternative wording, *"due to a transit delay on their end."* |
