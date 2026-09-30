"""Typed contracts between pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Decision(str, Enum):
    DRAFT = "draft"
    NEEDS_MANAGER_APPROVAL = "needs-manager-approval"
    DRAFT_UNVERIFIED = "draft-unverified"
    NEEDS_HUMAN = "needs-human"


LABELS = {
    Decision.DRAFT: "cs/ready-to-send",
    Decision.NEEDS_MANAGER_APPROVAL: "cs/needs-approval",
    Decision.DRAFT_UNVERIFIED: "cs/draft-unverified",
    Decision.NEEDS_HUMAN: "cs/no-draft",
}

# Not decision-derived: applied via Outcome.label_override when the phishing net fires,
# so suspected scams are visibly quarantined in the Gmail UI. Listed here so the live
# mailbox creates it alongside the others.
PHISHING_LABEL = "cs/possible-phishing"

# What the reviewer should understand each label to mean.
LABEL_MEANING = {
    Decision.DRAFT: "Grounded and lint-clean. Read it and send it.",
    Decision.NEEDS_MANAGER_APPROVAL: "Drafted, but approve the exception or do the action first.",
    Decision.DRAFT_UNVERIFIED: "Draft written but a check FAILED. Starting point only — the "
    "flagged claim is unsupported. Do not send as-is.",
    Decision.NEEDS_HUMAN: "No draft. Nothing grounded to say, or it must never be automated.",
}

# Decisions that still put a draft in front of a human.
WRITES_DRAFT = {Decision.DRAFT, Decision.NEEDS_MANAGER_APPROVAL, Decision.DRAFT_UNVERIFIED}


@dataclass
class Email:
    message_id: str
    thread_id: str
    sender: str
    subject: str
    body: str
    # True when our own last reply promised to come back to her ("let me pull the
    # measurements…") and never did. The thread looks answered by timestamps; it isn't.
    followup_owed: bool = False

    @property
    def raw_text(self) -> str:
        return f"{self.subject}\n\n{self.body}"


@dataclass
class Signals:
    """Booleans the classifier observes. It does NOT decide anything with them."""

    angry_or_threatening: bool = False
    demands_refund_or_cancel: bool = False
    legal_press_vip: bool = False
    non_english: bool = False
    time_sensitive_deadline: bool = False
    delivery_dispute: bool = False
    asks_about_money_amount: bool = False
    photos_already_attached: bool = False
    possible_phishing_or_scam: bool = False

    def as_dict(self) -> dict[str, bool]:
        return self.__dict__.copy()

@dataclass
class Classification:
    signals: Signals
    rationale: str
    category: str
    risk: str  # low | medium | high
    order_identifier: str | None = None
    customer_first_name: str | None = None
    country: str | None = None            # ship-to country, only if she stated it
    product_mentioned: str | None = None  # a specific piece she named, verbatim-ish


@dataclass
class Routing:
    decision: Decision
    reason: str
    triggered_by: str  # "safety-net" | "signal" | "risk" | "category" | "lookup" | "verify"


@dataclass
class OrderFacts:
    """Exactly the Shopify fields retrieved for this message. The drafter sees nothing else."""

    found: bool
    order_id: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def groundable_values(self) -> list[str]:
        # Recurse: garment measurements and price facts arrive as nested dicts, and a
        # number the drafter may quote must be findable by verify's layer-1 haystack.
        def flatten(v) -> list[str]:
            if v is None:
                return []
            if isinstance(v, dict):
                return [s for x in v.values() for s in flatten(x)]
            if isinstance(v, (list, tuple)):
                return [s for x in v for s in flatten(x)]
            return [str(v)]

        out = flatten(dict(self.fields))
        if self.order_id:
            out.append(str(self.order_id))
        return out


@dataclass
class Draft:
    text: str
    flag_for_review: bool = False


@dataclass
class VerifyResult:
    grounded: bool
    layer1_violations: list[str] = field(default_factory=list)
    layer2_violations: list[str] = field(default_factory=list)
    layer2_ran: bool = False

    @property
    def all_violations(self) -> list[str]:
        return self.layer1_violations + self.layer2_violations


@dataclass
class Outcome:
    message_id: str
    decision: Decision
    reason: str
    triggered_by: str
    category: str | None = None
    risk: str | None = None
    signals: dict[str, bool] = field(default_factory=dict)
    order_facts: dict[str, Any] = field(default_factory=dict)
    draft_text: str | None = None
    verify: VerifyResult | None = None
    lint_violations: list[str] = field(default_factory=list)
    error: str | None = None
    withheld: bool = False  # a draft was produced but never handed to Gmail
    # Set to override the decision-derived Gmail label — the phishing net uses this so a
    # flagged email is visibly quarantined in the mailbox UI, not just "no draft".
    label_override: str | None = None

    @property
    def label(self) -> str:
        return self.label_override or LABELS[self.decision]
