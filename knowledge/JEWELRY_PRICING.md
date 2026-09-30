# Jewelry pricing — why customers email, and how we stop them having to

## The problem, in one paragraph

Shopify will not display a price on our diamond jewelry product pages. The workaround was to
duplicate those pages with the price set to **$0**, which is what a customer now sees. So a
steady stream of international customers write in asking what a piece costs, and someone
types a price by hand — looking it up, converting it, adding shipping, adding duties, and
hoping they got all four right. It's a meaningful share of CS volume and every reply is a
chance to quote a wrong number.

## What shipped (2026-08-23): the price is retrieved live, per country, at draft time

`ShopifyAdminClient.price_lookup(product, country)` reads every variant's
`contextualPricing` for the customer's stated country (she always states it), discards the
zeros per the sentinel rule below, and quotes min–max of what remains — in the market's own
currency (verified live: Italy → EUR, UK → GBP). The classifier extracts her country and
the piece she named; `jewelry_price_inquiry` drafts at **manager tier**, so a human confirms
every number before it goes out, and verify withholds any draft whose price isn't the
retrieved one. No country or no piece named → the draft asks for them, exactly like the
real corpus reply.

## Phase 1 page goal (now the self-serve follow-up)

**A dedicated price page per country.** Customer service answers by dropping in the right
link instead of typing a price. Superseded as the *blocker* by the live lookup above, but
still worth building for self-serve.

That is a content problem, not a pipeline problem, and it has a second benefit that is easy
to miss: **a link is not a claim.** The drafting pipeline's grounding layer blocks any draft
that states an amount it can't source (`verify.py` layer 1). A price typed into an email has
to be verified; a link to a maintained page doesn't. Sending a link is the only version of
this that a drafter can ever be allowed to do unsupervised.

### Current state

| | |
| --- | --- |
| Built | Italy — but it lives at a `claude.ai/code/artifact/...` URL, **which we cannot send to customers**, and its shipping input is a $75 placeholder. |
| Pending | UK, France, Germany, Switzerland, Sweden, Greece, Belgium, Poland, Australia, New Zealand, Canada |

Until at least one sheet exists on an AFLALO-controlled URL there is **no sendable link for
any country**, which is why `jewelry_price_inquiry` is a `human` tier category in
`categories.yaml` and `drafter_may_quote_a_price` is `false` in `policy_facts.yaml`. Nothing
about that changes on the pipeline side; it changes when the pages exist.

---

## Reading the price out of Shopify

The country pages should be generated from Shopify, not maintained by hand, or they drift
the first time a price changes.

Needs the `read_products` scope on the Admin app — one more than the three the order lookup
uses (`read_orders`, `read_customers`, `read_inventory`).

```graphql
query Piece($handle: String!) {
  productByHandle(handle: $handle) {
    id title handle status
    priceRangeV2 {
      minVariantPrice { amount currencyCode }
      maxVariantPrice { amount currencyCode }
    }
    variants(first: 50) {
      edges { node { id title sku price availableForSale } }
    }
  }
}
```

### The $0 duplicate is a sentinel, not a price

`price` comes back as a **string** — `"4250.00"`, `"0.00"`. The zero is the price-suppressed
duplicate page, not a free product.

**The rule: parse every price to a number, discard the zeros, and take the price from what
is left.** A `0` must never reach a customer-facing page and must never be treated as
"price unavailable, show blank" either — it means "this is the duplicate; the real record is
elsewhere".

```
prices = [to_number(v.price) for v in variants]
real   = [p for p in prices if p > 0]

if not real:          -> no price for this piece. Escalate; do NOT publish a page for it.
if len(set(real)) == 1 -> single price
else                   -> a range: min(real) .. max(real)
```

Two open points to settle against the live catalog before this is implemented, because the
answer changes the matching logic:

1. **How the duplicate is identified.** The working note says the identifier "has an INT at
   the end" — Shopify appends a numeric suffix when a handle or title is duplicated
   (`emerald-solitaire-ring-1`). If that suffix is the reliable marker, match on it and
   prefer the un-suffixed original. **Do not rely on the suffix alone**: `-1` also shows up
   in legitimately renamed products. Treat non-zero price as the primary signal and the
   suffix as a cross-check — if the two disagree on the same piece, that piece needs a human
   before it goes on a page.
2. **Whether the duplicate or the original is the page customers actually land on.** If
   customers reach the $0 duplicate, the country page has to be linked from *that* handle,
   and the price has to be read from its non-suppressed twin.

### Prices are a range, and they move

Two separate reasons a single number is the wrong output:

- **A piece spans variants** — sizes, carat weights, metals — so its honest answer is a
  range, `priceRangeV2` min to max.
- **Diamond and metal prices move.** A quote given today is not a commitment for next month.

Every country page and every reply that references one carries a disclaimer to that effect.
Wording to be signed off by Emily, but the substance is fixed:

> Prices shown are current and can vary with the diamond and metal market — the final price
> is the one confirmed at the time of purchase.

This is not boilerplate. It is what makes a published price safe to send, and it is the
difference between a reference and a quote.

### What a country page needs beyond the price

The Italy pilot shows the shape. Each of these is an input that can be wrong independently,
so each one is worth naming rather than folding into a single number:

| Input | Note |
| --- | --- |
| Base price / range | From Shopify, per the rules above. |
| Currency | Local, with the conversion basis stated. |
| Shipping | The Italy sheet's **$75 is a placeholder** — it must be replaced with the real rate before that page is republished. |
| Duties / VAT | Country-specific, and the single most common thing a customer is actually asking about. |
| Disclaimer | Above. Non-optional. |
| Last updated | A stale price page is worse than no price page. |

---

## Sequence

1. Republish the Italy sheet on an AFLALO-controlled URL — a real page or a PDF, not a
   `claude.ai` artifact. This unblocks *the concept*, not just Italy.
2. Replace the $75 shipping placeholder with the real Italy rate.
3. Confirm the two open points above against the live catalog with `read_products`.
4. Generate the remaining 11 country pages from Shopify rather than by hand.
5. Only then: move `jewelry_price_inquiry` off `human` tier — to a draft that **sends the
   link and quotes no number**. The category never gets permission to state a price.
