"""[3] Classifier — the model OBSERVES. It does not decide draft-vs-escalate.

Schema field order is deliberate: signals -> rationale -> category -> risk. Under structured
output the model fills fields in order, so putting the observations and the reasoning before
the category/risk commitment pushes it to reason first rather than snap to a label.
"""

from __future__ import annotations

from . import knowledge
from .llm import LLM, cached_system
from .models import Classification, Email, Signals

SYSTEM = """You are triaging inbound customer service email for AFLALO, a fashion and fine
jewelry brand. Your only job is to OBSERVE and describe the message. You do not decide what
happens to it — separate deterministic code does that from your output.

Be conservative. When a signal is arguable, set it to true. A false positive costs a human
thirty seconds of reading; a false negative sends a customer the wrong email.

Signal definitions:
- angry_or_threatening: frustrated, escalating, sarcastic, or threatening any action.
- demands_refund_or_cancel: explicitly asks to cancel an order or be refunded.
- legal_press_vip: mentions lawyers, press, an influencer/PR/VIP relationship, or a
  connection to the brand.
- non_english: any part of the message is not in English.
- time_sensitive_deadline: names a date or event she needs the item by.
- delivery_dispute: she disputes what tracking says — marked delivered but never arrived,
  or she believes it is lost. A package that is merely running late is NOT a dispute; that
  is an ordinary delayed-package question and the pipeline has a category for it.
- asks_about_money_amount: asks what something costs, how much she'll be refunded, about a
  fee, or about a discount.
- photos_already_attached: she says she has attached or is attaching photos.
- possible_phishing_or_scam: the message pattern-matches a con — someone claiming to
  represent a celebrity/brand asking for product from a personal email address, sender
  identity not matching who they claim to be, requests to change payment details, links
  demanding account verification, or a too-good offer. Set it on the PATTERN; a human
  verifies. Real stylist pull requests look like fake ones — that is exactly why.

Extract order_identifier only if an order number actually appears in the message. Extract
customer_first_name only from a signature or an explicit self-introduction — never guess it
from an email address.

Extract country if she states or clearly implies her location or ship-to country ("I'm in
Italy", "shipping to the UK", "does this price include duties for Canada") — the country
name as she wrote it, else null. Extract product_mentioned if she names a specific piece or
style ("the Lone Diamond Pendant Necklace", "Tavi pant") — the product name as she wrote
it, else null. Never infer either from the email domain or from guesswork.

Risk is your read of how costly it would be to send this customer a wrong reply: low for
routine informational questions, medium when money or a policy exception is nearby, high when
the relationship already looks strained or the situation is unclear."""


def _schema(categories: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "signals": {
                "type": "object",
                "properties": {k: {"type": "boolean"} for k in Signals().as_dict()},
                "required": list(Signals().as_dict().keys()),
                "additionalProperties": False,
            },
            "rationale": {"type": "string"},
            "category": {"type": "string", "enum": categories},
            "risk": {"type": "string", "enum": ["low", "medium", "high"]},
            "order_identifier": {"type": ["string", "null"]},
            "customer_first_name": {"type": ["string", "null"]},
            "country": {"type": ["string", "null"]},
            "product_mentioned": {"type": ["string", "null"]},
        },
        "required": [
            "signals",
            "rationale",
            "category",
            "risk",
            "order_identifier",
            "customer_first_name",
            "country",
            "product_mentioned",
        ],
        "additionalProperties": False,
    }


def classify(llm: LLM, email: Email) -> Classification:
    cats = knowledge.category_names()
    catalogue = "\n".join(
        f"- {name}: {knowledge.get_category(name).get('approach', '') or knowledge.get_category(name).get('reason_for_tier', '')}".strip()[
            :200
        ]
        for name in cats
    )
    system = cached_system(SYSTEM, f"Valid categories:\n{catalogue}")
    user = f"Subject: {email.subject}\n\nFrom: {email.sender}\n\nBody:\n{email.body}"

    raw = llm.structured(
        system=system, user=user, schema=_schema(cats), effort="medium", max_tokens=2048
    )
    return Classification(
        signals=Signals(**raw["signals"]),
        rationale=raw["rationale"],
        category=raw["category"],
        risk=raw["risk"],
        order_identifier=raw.get("order_identifier") or None,
        customer_first_name=raw.get("customer_first_name") or None,
        country=raw.get("country") or None,
        product_mentioned=raw.get("product_mentioned") or None,
    )
