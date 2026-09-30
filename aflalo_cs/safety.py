"""[2] Hard safety net — deterministic, runs on raw text before the model is trusted at all.

Deliberately not model-based: this is the one class of email where a classifier mistake is
most costly, so the backstop must not depend on the same kind of judgment it exists to catch.
"""

from __future__ import annotations

import re

PATTERNS: list[tuple[str, str]] = [
    (r"\battorney\b|\blawyer\b|\blegal counsel\b|\bcounsel\b", "legal representation mentioned"),
    (r"\blawsuit\b|\bsuing\b|\bsue you\b|\blitigation\b|\bsmall claims\b", "litigation threatened"),
    (r"\bchargeback\b|\bcharge ?back\b", "chargeback mentioned"),
    (r"\bdispute the charge\b|\bdisputing the charge\b|\bdispute this charge\b", "payment dispute"),
    (r"\bbetter business bureau\b|\bBBB\b", "regulatory complaint"),
    (r"\battorney general\b|\bconsumer protection\b|\bFTC\b", "regulatory complaint"),
    (r"\bfraud\b|\bfraudulent\b|\bscam\b", "fraud allegation"),
    (r"\bclass action\b", "class action mentioned"),
    (r"\bcease and desist\b", "cease and desist"),
    (r"\bGDPR\b|\bCCPA\b|\bdelete my data\b|\bright to be forgotten\b", "data-protection request"),
    (r"\bpress\b|\bjournalist\b|\breporter\b|\bmagazine\b", "press contact"),
    (r"\bchargeback\b", "chargeback mentioned"),
]

_COMPILED = [(re.compile(p, re.IGNORECASE), why) for p, why in PATTERNS]


def check(raw_text: str) -> str | None:
    """Return a human-readable reason if the email must skip the model entirely."""
    for rx, why in _COMPILED:
        m = rx.search(raw_text)
        if m:
            return f"{why} (matched {m.group(0)!r})"
    return None
