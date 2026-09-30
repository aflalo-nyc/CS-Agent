"""The real voice — actual sent replies as tone exemplars for the drafter.

`brand_voice.md` is the deck's *description* of the voice; the sent replies in the store are
the voice itself. Design doc §3.3 calls them the gold-standard reference, and until now they
sat unused. The drafter gets the 2–3 most relevant ones alongside the deck rules.

The exemplars are tone reference ONLY. Every fact inside them belongs to a different
customer, and that is not left to trust: exemplar facts are not in `OrderFacts.fields`, so a
draft that copies one fails verify layer 1/2 as an ungrounded claim.

Observed conflict, recorded not resolved: the real corpus signs "Best, Eva" while the deck
mandates "Warmly, Eva". The deck is the designated source of truth, so lint still enforces
"Warmly" — but this belongs in knowledge/CONFLICTS.md with the other deck-vs-reality gaps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Email

# Footer/boilerplate lines in the real corpus: bare URLs in angle brackets, the SoHo street
# address block, and Google-Maps links, all appended by the mail client's signature.
FOOTER_LINE_RE = re.compile(
    r"^\s*<?https?://\S+>?\s*$"
    r"|aflalonyc\.com|google\.com/maps"
    r"|^\s*56 Greene St(reet)?\b|^\s*New York, NY 10012\s*$|^\s*Floor \d\s*$",
    re.IGNORECASE,
)

# Quoted history the import's stripper missed: Gmail wraps "On <date> ... <addr> wrote:"
# across lines, so the address (and "wrote:") can land on the next line. Cut at the date
# form itself. Forwarded blocks cut at the marker.
QUOTE_START_RE = re.compile(
    r"\nOn (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"|\n-{3,}\s*Forwarded message\s*-{3,}"
    r"|\n-{2,} ?Original Message",
)

GREETING_RE = re.compile(r"^\s*(hi|hello|hey|dear)\b", re.IGNORECASE)
SIGNOFF_RE = re.compile(r"^\s*(warmly|best regards|best|kind regards|thank you|thanks)\s*[,!.]?\s*$", re.IGNORECASE)

STOPWORDS = frozenset(
    "a an and are as at be but by for from has have i if in is it me my of on or our so "
    "that the this to was we what when where which will with would you your please hi hello "
    "thanks thank am do does did can could just there here about".split()
)


def clean_reply(body: str) -> str | None:
    """One sent reply -> exemplar text, or None if nothing usable survives.

    Usable = a greeting, a signoff, and real prose in between. That shape-check is what
    rejects internal notes (Lillian asking Ops a question), footer-only bodies, and
    forwarded fragments — none of which are the customer-facing voice.
    """
    text = body.replace("\r\n", "\n")
    cut = QUOTE_START_RE.search(text)
    if cut:
        text = text[: cut.start()]

    lines = [ln for ln in text.split("\n") if not FOOTER_LINE_RE.search(ln)]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

    if not GREETING_RE.match(text):
        return None
    if not any(SIGNOFF_RE.match(ln) for ln in text.split("\n")):
        return None
    prose = re.sub(r"\s+", " ", text)
    if len(prose) < 60 or len(prose) > 2500:
        return None
    return text


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z']+", text.lower()) if w not in STOPWORDS and len(w) > 2}


@dataclass
class Exemplar:
    subject: str
    text: str
    tokens: set[str]


def prepare(corpus: list[dict]) -> list[Exemplar]:
    """Clean the whole corpus once per run. `corpus` is Store.voice_corpus() rows."""
    out: list[Exemplar] = []
    for row in corpus:
        cleaned = clean_reply(row.get("body") or "")
        if cleaned:
            subject = row.get("subject") or ""
            out.append(Exemplar(subject=subject, text=cleaned, tokens=_tokens(f"{subject} {cleaned}")))
    return out


def top_k(exemplars: list[Exemplar], email: Email, k: int = 3) -> list[str]:
    """The k most relevant real replies for this email, by plain token overlap.

    120 documents does not need embeddings. Zero overlap still returns the most recent
    exemplars — general tone is better than no tone, and the corpus is newest-first.
    """
    if not exemplars:
        return []
    want = _tokens(f"{email.subject} {email.body}")
    scored = sorted(
        ((len(want & ex.tokens), i, ex) for i, ex in enumerate(exemplars)),
        key=lambda t: (-t[0], t[1]),
    )
    return [ex.text[:800] for _, _, ex in scored[:k]]
