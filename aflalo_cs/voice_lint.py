"""Deterministic voice lint — free, instant, runs on every draft.

Catches the most literal DO/DON'T violations from the CS deck before a human ever spends
review time on them, and before a human scorer spends a rating on voice.
"""

from __future__ import annotations

import re

SIGNOFF = "Warmly, Eva"
_SIGNOFF_RE = re.compile(r"warmly,\s*\n?\s*eva\s*$", re.IGNORECASE)

# The em dash is a tell: the real sent corpus doesn't use it, drafts kept reaching for it.
EM_DASH_RE = re.compile(r"[\u2014\u2013]")

BANNED = [
    (re.compile(r"dear valued customer", re.I), 'uses "Dear Valued Customer"'),
    (re.compile(r"\bwe apologize for any inconvenience this may have caused\b", re.I), "form-letter apology"),
    (re.compile(r"\bplease do not hesitate\b", re.I), "form-letter phrasing"),
    (re.compile(r"\bcontact (?:the carrier|UPS|FedEx|USPS)\b", re.I), "tells her to contact the carrier"),
    (re.compile(r"\breach out to (?:the carrier|UPS|FedEx|USPS)\b", re.I), "tells her to contact the carrier"),
    (re.compile(r"\bas an AI\b|\bI am an AI\b|\blanguage model\b", re.I), "breaks character"),
    (re.compile(r"\bwe are writing to inform you\b", re.I), "corporate register"),
]

APOLOGY_RE = re.compile(r"\b(?:i'?m sorry|i apologi[sz]e|we apologi[sz]e|my apologies|so sorry)\b", re.I)

# Reviewed drafts kept growing a comfort sentence after the answer ("If anything looks
# off with the tracking, just tell me and I'll take it from there"). The point is to sound
# like a person, and a person stops when they've answered.
TRAILING_FLUFF_RE = re.compile(
    r"if anything (?:looks|seems) off|just (?:tell|let) me know|i'?m right here"
    r"|don'?t hesitate|feel free to|anything else you need|if there'?s anything else"
    r"|happy to help if|come (?:by|visit) (?:our|the) (?:showroom|store)",
    re.IGNORECASE,
)

# The deck: "One line of warmth is enough. She does not need to be hand-held."
# Two or more open invitations in one email is warmth theater — found in live drafts
# ("tell me and I'll help you compare... and if you have a size in mind, let me know...").
INVITATION_RE = re.compile(
    r"\b(?:let me know|tell me|feel free to|don'?t hesitate to|happy to help (?:you )?(?:with|if)|"
    r"if there'?s anything else)\b",
    re.I,
)


def lint(text: str, *, hard_no: bool = False) -> list[str]:
    """Return a list of violations. Empty means the draft passes.

    hard_no: set for the scenarios where the deck says "unfortunately" is appropriate
    (final sale, a genuine firm refusal).
    """
    problems: list[str] = []
    stripped = text.strip()

    if not _SIGNOFF_RE.search(stripped):
        problems.append(f'missing required sign-off "{SIGNOFF}"')

    for rx, why in BANNED:
        if rx.search(text):
            problems.append(why)

    excls = text.count("!")
    if excls > 1:
        problems.append(f"{excls} exclamation points (max 1)")

    dashes = EM_DASH_RE.findall(text)
    if dashes:
        problems.append(
            f"{len(dashes)} em/en dash(es) — not our punctuation; use a period or comma"
        )

    for fluff in dict.fromkeys(TRAILING_FLUFF_RE.findall(text)):
        problems.append(f"trailing filler ({fluff!r}) — the draft ends when the answer ends")

    invites = INVITATION_RE.findall(text)
    if len(invites) > 1:
        problems.append(
            f"{len(invites)} separate invitations ({', '.join(repr(i) for i in invites[:3])}) — "
            "the deck says one line of warmth is enough; keep ONE closing offer"
        )

    first_line = next((l for l in stripped.splitlines() if l.strip()), "")
    body_after_greeting = stripped.split("\n", 1)[1] if "\n" in stripped else ""
    opener = next((l for l in body_after_greeting.splitlines() if l.strip()), first_line)
    if not hard_no and re.match(r"\s*unfortunately\b", opener, re.I):
        problems.append('opens with "unfortunately" on a scenario that is not a hard no')

    apologies = len(APOLOGY_RE.findall(text))
    if apologies > 1:
        problems.append(f"over-apologizes ({apologies} apologies — once is enough)")

    return problems
