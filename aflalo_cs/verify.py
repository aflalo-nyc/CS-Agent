"""[8] Verify — a two-layer grounding guardrail.

Layer 1 is a deterministic scan for the *kinds* of tokens that are dangerous to invent, and
confirms each one traces back to retrieved data. Fast, free, always runs, unit-testable
on its own.

Layer 2 is a second, narrow model call whose only job is fact-checking the draft against the
retrieved data — it catches prose-level claims the pattern scan can't, like "your order has
shipped" written over a status of "unfulfilled".

Either layer failing withholds the draft entirely. Never a partial or best-guess draft.
"""

from __future__ import annotations

import json
import re

from .llm import LLM, LLMError, cached_system
from .models import Draft, OrderFacts, VerifyResult

# Tokens that must trace back to retrieved data.
TRACKING_RE = re.compile(r"\b1Z[0-9A-Z]{16}\b|\b\d{12,22}\b")
ORDER_RE = re.compile(r"#\s?(\d{3,7})\b")
MONEY_RE = re.compile(r"[$€£]\s?\d[\d,]*(?:\.\d{2})?|\b\d[\d,]*(?:\.\d{2})?\s?(?:USD|EUR|GBP|CHF|CAD|AUD)\b")
DATE_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,\s*\d{4})?\b"
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"
    r"|\b\d{4}-\d{2}-\d{2}\b",
    re.IGNORECASE,
)
ATTRIBUTE_RE = re.compile(
    r"\b(?:white gold|yellow gold|rose gold|gold-plated|platinum|sterling silver|silver|gold|"
    r"diamond|emerald|sapphire|ruby|pearl|tahitian)\b",
    re.IGNORECASE,
)
# Claims about state that must match fulfillment_status.
SHIPPED_CLAIM_RE = re.compile(
    r"\b(?:has shipped|have shipped|is on its way|on its way to you|shipped out|in transit|"
    r"out for delivery)\b",
    re.IGNORECASE,
)
# Absence of data is not evidence of not-shipped. If fulfillment_status was never retrieved
# (the category didn't ask for it), we have nothing to contradict — skip the check rather
# than blocking a correct draft.
UNSHIPPED_STATUSES = {"unfulfilled", "on_hold", "scheduled", "pending"}
# {name} or [bracketed words] left in a draft. A bracket followed by "(" is a markdown
# link, not a placeholder (the drafter is told not to write those, but the lint for it is
# voice, not grounding).
PLACEHOLDER_RE = re.compile(r"\{[a-z_]+\}|\[[A-Za-z ][^\]]{0,40}\](?!\()")

# A discount code is a money commitment requiring Lillian's authorization. The drafter may
# never offer one, so any code-shaped token in a draft is a violation regardless of data.
DISCOUNT_CODE_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,14}-?\d{1,3}(?:OFF|%)\b|\b[A-Z]{2,}-\d{1,3}OFF\b")

# A named carrier facility/location. The deck's delayed-package template has exactly this
# placeholder ("held up at their facility in X"), which makes it the most likely thing to
# get invented when carrier_location wasn't retrieved.
FACILITY_RE = re.compile(
    r"\bfacility (?:in|at) ([A-Z][\w.]*(?:[ ,]+[A-Z][\w.]*)*)"
    r"|\b(?:hub|depot|sorting (?:center|centre|facility)) (?:in|at) ([A-Z][\w.]*)",
)

# Relative/weekday delivery promises carry no numerals, so the numeric DATE_RE misses them.
WEEKDAY_PROMISE_RE = re.compile(
    r"\b(?:by|on|arrive[sd]?|delivered)\s+(?:this |next |the )?"
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|tomorrow)\b",
    re.IGNORECASE,
)
DATE_FIELDS = ("estimated_delivery", "estimated_ship_date")


def _haystack(facts: OrderFacts) -> str:
    return " | ".join(facts.groundable_values()).lower()


def layer1(draft: Draft, facts: OrderFacts, policy: str = "") -> list[str]:
    """Deterministic scan. No model call.

    `policy` joins the haystack: published policy amounts (the $20/$50 return fee, the
    14-day window) are stateable facts with the site as source, and without this a draft
    correctly citing the published fee would be withheld as an invented amount.
    """
    text = draft.text
    hay = _haystack(facts) + " | " + policy.lower()
    violations: list[str] = []

    def ungrounded(matches, label: str, normalize=lambda s: s.lower()) -> None:
        for m in dict.fromkeys(matches):  # de-dupe, keep order
            if normalize(m) not in hay:
                violations.append(f"ungrounded {label}: {m!r}")

    ungrounded(TRACKING_RE.findall(text), "tracking number")
    ungrounded(ORDER_RE.findall(text), "order number")
    ungrounded(
        MONEY_RE.findall(text),
        "amount",
        lambda s: re.sub(r"[\s$€£,]|USD|EUR|GBP|CHF|CAD|AUD", "", s),
    )
    ungrounded(DATE_RE.findall(text), "date")
    ungrounded(ATTRIBUTE_RE.findall(text), "product attribute")

    for ph in PLACEHOLDER_RE.findall(text):
        violations.append(f"unfilled placeholder left in draft: {ph!r}")

    status = (
        str(
            facts.fields.get("_raw_fulfillment_status")
            or facts.fields.get("fulfillment_status")
            or ""
        ).lower()
        or None
    )
    if status is not None and SHIPPED_CLAIM_RE.search(text) and status in UNSHIPPED_STATUSES:
        claim = SHIPPED_CLAIM_RE.search(text).group(0)
        violations.append(
            f"draft claims {claim!r} but retrieved fulfillment_status is {status!r}"
        )

    for code in dict.fromkeys(DISCOUNT_CODE_RE.findall(text)):
        violations.append(f"draft offers a discount code ({code!r}) — always a human decision")

    if not facts.fields.get("carrier_location"):
        for groups in FACILITY_RE.findall(text):
            named = next((g for g in groups if g), None)
            if named:
                violations.append(f"names a carrier facility ({named!r}) that was not retrieved")

    if not any(facts.fields.get(f) for f in DATE_FIELDS):
        m = WEEKDAY_PROMISE_RE.search(text)
        if m:
            violations.append(f"promises a delivery day ({m.group(0)!r}) with no retrieved date")

    return violations


LAYER2_SYSTEM = """You are fact-checking a drafted customer service email. That is your only job.

A draft may legitimately draw on THREE sources. Check each claim against all three before
calling it ungrounded:

1. RETRIEVED ORDER DATA — facts about this specific order. The only source for order status,
   tracking, dates, amounts, and product attributes.
2. THE CUSTOMER'S OWN EMAIL — anything she stated. Reflecting her report back to her is
   grounded ("the piece arrived damaged", "the wrong item", naming a product she named).
   The draft must not upgrade her report into a confirmed finding, but repeating it is fine.
3. AFLALO POLICY — standing policy given below. Stating policy is grounded.

Also grounded: general courtesy with no factual content ("thank you for your patience"), and
commitments about what AFLALO will do next ("I'll follow up tomorrow", "I'll send tracking
when it ships").

NOT grounded: anything specific about the order, shipment, product, amount, or date that
none of the three sources supports — including claims that merely go further than the data
(saying it shipped when the status is processing, naming a facility not retrieved, promising
a date not present), and unverifiable embellishment ("one of our most loved pieces", "I've
added you to the waitlist") where nothing confirms it.

FILL-IN BLANKS: a run of underscores (______) is a deliberate blank the human will fill in
before sending. It is not a claim and must never be flagged, nor the sentence for containing
it — "The updated ship date is ______." is exactly how the drafter is told to handle a fact
it doesn't have. Still flag any concrete assertion around a blank that no source supports.

Return grounded=false with one short violation string per problem. Withholding a good draft
costs a human two minutes; sending a wrong one costs a customer's trust."""

LAYER2_SCHEMA = {
    "type": "object",
    "properties": {
        "grounded": {"type": "boolean"},
        "violations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["grounded", "violations"],
    "additionalProperties": False,
}


ACTION_GATED_NOTE = """
IMPORTANT — this draft is ACTION-GATED. It will not be sent until a human has performed the
remedy it describes (shipping the replacement, processing the refund, attaching the label,
adding her to the waitlist). Statements describing that pending remedy are therefore expected
and grounded — do NOT flag "I've added you to the waitlist", "the correct item is going out",
"a refund has been issued", "a return label is attached", or an acknowledgement that we made
a mistake she reported.

Order FACTS are still held to the same standard: tracking numbers, dates, amounts, product
attributes and order status must still come from the retrieved data. Unverifiable
embellishment about the product ("one of our most loved pieces") is still ungrounded.
"""


def layer2(
    llm: LLM,
    draft: Draft,
    facts: OrderFacts,
    customer_email: str = "",
    policy: str = "",
    action_gated: bool = False,
) -> list[str]:
    payload = (
        "1. RETRIEVED ORDER DATA:\n"
        f"{json.dumps(facts.fields, indent=2) if facts.fields else '(no order data retrieved for this category)'}\n\n"
        "2. THE CUSTOMER'S OWN EMAIL:\n"
        f"{customer_email or '(not provided)'}\n\n"
        "3. AFLALO POLICY:\n"
        f"{policy or '(not provided)'}\n\n"
        f"DRAFT TO CHECK:\n{draft.text}"
    )
    raw = llm.structured(
        system=cached_system(LAYER2_SYSTEM + (ACTION_GATED_NOTE if action_gated else "")),
        user=payload,
        schema=LAYER2_SCHEMA,
        effort="medium",
        max_tokens=1024,
    )
    return [] if raw["grounded"] else list(raw["violations"]) or ["model reported ungrounded"]


def verify(
    llm: LLM | None,
    draft: Draft,
    facts: OrderFacts,
    customer_email: str = "",
    policy: str = "",
    action_gated: bool = False,
) -> VerifyResult:
    l1 = layer1(draft, facts, policy)
    l2: list[str] = []
    ran = False
    if llm is not None:
        try:
            l2 = layer2(llm, draft, facts, customer_email, policy, action_gated)
            ran = True
        except LLMError as exc:
            # A verifier that couldn't run is not a pass.
            l2 = [f"layer 2 fact-check could not run: {exc}"]
    return VerifyResult(
        grounded=not (l1 or l2), layer1_violations=l1, layer2_violations=l2, layer2_ran=ran
    )
