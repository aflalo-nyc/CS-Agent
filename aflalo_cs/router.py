"""[4] Router — ordinary deterministic code, NOT a model call.

Every escalation traces to one named rule, so it is auditable and unit-testable
independent of model behaviour.
"""

from __future__ import annotations

from . import knowledge
from .models import Classification, Decision, OrderFacts, Routing

# Any of these signals set -> a human reads it. Ordered: first match wins, so the logged
# reason is stable and specific rather than "several signals".
ESCALATING_SIGNALS: list[tuple[str, str]] = [
    ("possible_phishing_or_scam",
     "possible phishing/scam pattern — do not reply until the sender is verified"),
    ("angry_or_threatening", "angry/threatening signal set"),
    ("asks_about_money_amount", "asks about a specific amount, fee, or refund total"),
    ("non_english", "non-English message"),
]

# These force MANAGER tier instead of stripping the draft: the human decides, but from a
# starting point. Heat is different — anger stays hard-escalating above.
SOFT_GATE_SIGNALS: list[tuple[str, str]] = [
    ("demands_refund_or_cancel",
     "she asks for a refund/cancellation — the remedy is a manager decision; "
     "the draft is a starting point, decide before sending"),
    ("time_sensitive_deadline",
     "she named a deadline — confirm feasibility before sending; the draft promises no date"),
    ("delivery_dispute",
     "she disputes delivery — verify the carrier record before sending"),
    # Press REQUESTS never reach here: the pre-classification safety net catches them
    # (magazine / press-contact patterns) and they stay human-only. This signal is what
    # remains — a makeup artist with an agency signature asking about her own order, a
    # fashion editor saying thanks (seen 2026-09-11). A customer with a notable job title
    # deserves a careful reply, not no reply.
    ("legal_press_vip",
     "industry/VIP signature — a manager reads this reply before it goes"),
]


def route(cls: Classification) -> Routing:
    """Decide from the classifier's observations alone. Shopify hasn't run yet."""
    if cls.risk == "high":
        return Routing(Decision.NEEDS_HUMAN, "classifier rated risk high", "risk")

    # A category may declare signal exemptions in categories.yaml — declarative, so each
    # one is visible next to the category it loosens. Example: every price question sets
    # asks_about_money_amount, so jewelry_price_inquiry (whose whole point is a price, and
    # whose number is retrieved and manager-gated) exempts exactly that one signal.
    # Never exemptable, whatever a category declares: a con dressed as a partnership
    # pitch must not receive the partnership skeleton's warm acknowledgment, and a
    # furious anyone goes to a person.
    NEVER_EXEMPT = {"possible_phishing_or_scam", "angry_or_threatening"}
    exempt = set(knowledge.get_category(cls.category).get("signal_exemptions", [])) - NEVER_EXEMPT
    for attr, reason in ESCALATING_SIGNALS:
        if attr not in exempt and getattr(cls.signals, attr, False):
            return Routing(Decision.NEEDS_HUMAN, reason, "signal")

    tier = knowledge.tier(cls.category)

    if tier == "human":
        why = knowledge.get_category(cls.category).get(
            "reason_for_tier", "category is not draftable"
        )
        return Routing(
            Decision.NEEDS_HUMAN,
            f"category '{cls.category}' is human-only: {why.strip().splitlines()[0]}",
            "category",
        )

    if knowledge.needs_order(cls.category) and not cls.order_identifier:
        # No order number is not a dead end — the correct reply IS asking for it. The
        # drafter answers whatever is answerable without the order, plus one question.
        return _gate_soft_signals(cls, Routing(
            Decision.DRAFT,
            f"category '{cls.category}' needs an order number and none was given — "
            "the draft asks for it",
            "lookup",
        ))

    # damaged_item drafts a photo request. If she already attached photos, the next step is a
    # real replacement — same shape as any action-gated category.
    if cls.category == "damaged_item" and cls.signals.photos_already_attached:
        return _gate_soft_signals(cls, Routing(
            Decision.NEEDS_MANAGER_APPROVAL,
            "damaged item with photos attached — human must ship the replacement, then send",
            "category",
        ))

    if tier == "manager":
        custom = knowledge.get_category(cls.category).get("manager_reason")
        reason = (
            f"category '{cls.category}': {custom}"
            if custom
            else f"category '{cls.category}': human must complete the action the draft describes "
            "(ship / refund / attach label / add to waitlist) before sending"
            if knowledge.gate(cls.category) == "action"
            else f"category '{cls.category}' commits a policy exception — manager approval "
            "required before it goes out"
        )
        return _gate_soft_signals(cls, Routing(Decision.NEEDS_MANAGER_APPROVAL, reason, "category"))

    routing = Routing(Decision.DRAFT, f"category '{cls.category}' is draftable", "category")
    return _gate_soft_signals(cls, routing)


def _gate_soft_signals(cls: Classification, routing: Routing) -> Routing:
    """Soft signals force manager tier instead of stripping the draft.

    Found in the field: a damaged-item report with photos attached asked 'could you
    please advise on a refund?' and got NOTHING, when the identical email minus the word
    'refund' would have received a gated draft. Money, deadlines, and delivery disputes
    stay human decisions — but the human starts from a draft instead of a blank page.
    A HEATED anything still goes straight to a person: anger sets its own signal, which
    remains hard-escalating and never exemptable.
    """
    if routing.decision is Decision.NEEDS_HUMAN:
        return routing
    exempt = set(knowledge.get_category(cls.category).get("signal_exemptions", []))
    for attr, note in SOFT_GATE_SIGNALS:
        if attr not in exempt and getattr(cls.signals, attr, False):
            return Routing(
                Decision.NEEDS_MANAGER_APPROVAL, f"{routing.reason} | {note}", "signal"
            )
    return routing


def route_after_lookup(routing: Routing, facts: OrderFacts) -> Routing:
    """A failed or ambiguous Shopify lookup always wins. Never let the drafter guess."""
    if facts.found:
        return routing
    return Routing(
        Decision.NEEDS_HUMAN,
        f"Shopify lookup failed: {facts.error or 'order not found'}",
        "lookup",
    )
