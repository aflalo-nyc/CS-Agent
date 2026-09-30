"""[2b] Impersonation / scam net — deterministic, explainable, runs before the model.

Built against a real specimen from the inbox (2026-08): a "Katy Perry premiere" loan
request opening from a professional-looking stylist domain, followed up by
`julianavargasr30@gmail.com`. That is the celebrity gifting-loan con: get product shipped
against a name, vanish. The catch is that REAL stylist pulls look exactly the same — the
con works by imitating them — so this module never renders a verdict. It names the marks,
blocks any draft (even a polite decline confirms a live, credulous inbox), and tells the
human what to verify before anything moves.

Two tiers of mark:
- STRONG — one alone flags: a lookalike of our own domain, or a banking-detail change
  (vendor-fraud pattern). These have no innocent reading.
- WEAK — two or more flag: each alone is everyday fashion-industry mail.
"""

from __future__ import annotations

import re

from .models import Email

OUR_DOMAIN = "aflalonyc.com"

FREEMAIL = frozenset(
    "gmail.com yahoo.com hotmail.com outlook.com aol.com icloud.com proton.me "
    "protonmail.com mail.com gmx.com yandex.com".split()
)

# Weak marks — the anatomy of a gifting/loan con, each common in legitimate mail too.
PRODUCT_OUT_RE = re.compile(
    r"\b(?:loan|borrow|pull|gift(?:ing)?|send (?:us|over|out))\b.{0,80}"
    r"\b(?:looks?|pieces?|samples?|items?|jewelry|dress(?:es)?|gowns?)\b",
    re.IGNORECASE | re.DOTALL,
)
EVENT_GLAMOUR_RE = re.compile(
    r"\b(?:premiere|red carpet|tour|photo ?shoot|editorial|styling|stylist|music video|"
    r"press day|award[s]? (?:show|season))\b",
    re.IGNORECASE,
)
DEADLINE_RE = re.compile(
    r"\b(?:next week|this week|tomorrow|urgent(?:ly)?|asap|by (?:mon|tues|wednes|thurs|fri|satur|sun)day|"
    r"in \d+ days?|\d{1,2}(?:st|nd|rd|th) of [A-Z][a-z]+)\b",
    re.IGNORECASE,
)
REPRESENTATION_RE = re.compile(
    r"\b(?:on behalf of|i represent|we represent|represented by|management|talent agency|"
    r"styling (?:team|studio)|her team|his team|their team)\b",
    re.IGNORECASE,
)

# Strong marks.
BANKING_CHANGE_RE = re.compile(
    r"\b(?:new|updated?|changed?) (?:bank(?:ing)?|payment|account|wire|remittance) "
    r"(?:details?|information|instructions?)\b",
    re.IGNORECASE,
)
CREDENTIAL_BAIT_RE = re.compile(
    r"\b(?:verify your (?:account|identity|payment)|password (?:expires?|reset)|"
    r"suspended account|confirm your (?:credentials|billing))\b",
    re.IGNORECASE,
)


def _domain(sender: str) -> str:
    m = re.search(r"@([\w.\-]+)", sender or "")
    return m.group(1).lower().rstrip(".") if m else ""


def _lookalike_of_ours(domain: str) -> bool:
    """`aflalo` anywhere in a domain that isn't ours is someone dressing up as us."""
    if not domain or domain == OUR_DOMAIN or domain.endswith("." + OUR_DOMAIN):
        return False
    return "aflalo" in domain.replace("-", "").replace(".", "")


def assess(email: Email, prior_senders: list[str] | None = None) -> list[str]:
    """Return the scam marks found — empty means unflagged.

    `prior_senders` are the From headers of earlier inbound messages in the same thread,
    which is how the specimen's giveaway surfaces: the follow-up arrives from a freemail
    address that never appeared in the original exchange.
    """
    text = f"{email.subject}\n{email.body}"
    domain = _domain(email.sender)
    marks: list[str] = []
    strong: list[str] = []

    if _lookalike_of_ours(domain):
        strong.append(f"sender domain {domain!r} imitates ours ({OUR_DOMAIN})")
    if BANKING_CHANGE_RE.search(text):
        strong.append("asks to change banking/payment details — classic vendor-fraud pattern")
    if CREDENTIAL_BAIT_RE.search(text):
        strong.append("credential bait (verify account / password reset phrasing)")

    if PRODUCT_OUT_RE.search(text):
        marks.append("requests product be sent out (loan/pull/gifting)")
    if EVENT_GLAMOUR_RE.search(text):
        marks.append("name-drops a celebrity event (premiere/tour/shoot)")
    if DEADLINE_RE.search(text):
        marks.append("deadline pressure")
    if REPRESENTATION_RE.search(text) and domain in FREEMAIL:
        marks.append(f"claims to represent someone, from a personal address (@{domain})")

    prior_domains = {_domain(s) for s in (prior_senders or []) if _domain(s)}
    if prior_domains and domain in FREEMAIL and domain not in prior_domains:
        marks.append(
            f"thread began at {'/'.join(sorted(prior_domains))} but this follow-up comes "
            f"from a personal address (@{domain})"
        )

    if strong:
        return strong + marks
    return marks if len(marks) >= 2 else []


def advice(marks: list[str]) -> str:
    """The reason line a human sees. Names the marks and the verification step —
    deliberately not a verdict, because real stylist pulls look exactly like fake ones."""
    return (
        "possible impersonation/scam — DO NOT reply or ship until the requester is "
        "verified through a known channel (agency main line, verified social, existing "
        "contact). Marks: " + "; ".join(marks)
    )
