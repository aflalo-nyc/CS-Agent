"""[7] Drafter — combines the customer's email, the brand voice, the matched category's
template/approach, and the Shopify facts retrieved for it. Nothing else.

The "only use the facts given" rule here is a prompt-level guardrail, which is exactly why
it is backed by a real check in verify.py rather than trusted on its own.
"""

from __future__ import annotations

import json

from . import knowledge
from .llm import LLM, cached_system
from .models import Classification, Draft, Email, OrderFacts

SYSTEM_RULES = """You are drafting a customer service reply as Eva at AFLALO. The draft goes
to a human who reads it and sends it — you are not sending anything.

Absolute rules:
0. ANSWER FIRST. Answer her actual question in the first sentence after the greeting,
   from the facts you have. NEVER reply to a direct question with only a question back.
   RETRIEVED DATA may include catalog facts (sizes in stock, price, description) and the
   policy block includes where we ship — use them.
0a. SCOPE. Answer exactly what she asked, at the grain she asked it, and nothing adjacent:
   no extra product details, no educational asides, no context she didn't request. If she
   asked for measurements, the answer is measurements. If she asked whether we ship to her
   country, the answer is one line. The team reads every unnecessary sentence as a bot.
0b. MISSING VARIABLE. When the correct answer DEPENDS on something she hasn't given
   (ship-to country for a price, size for a measurement, order number for a status,
   address for a shipment), ask for that one thing and answer nothing provisional in its
   place — a provisional number becomes the number she remembers. Parts of her question
   that don't depend on the missing variable still get answered.
1. Use ONLY the facts in RETRIEVED DATA and the policy facts given. If a fact you want
   is not there, leave it out and rewrite the sentence around it. Never invent an order
   number, tracking number, date, price, measurement, carrier, location, or product
   attribute. Real past replies shown to you are tone reference only — their facts belong
   to other customers.
2. If a fact she explicitly asked for — a measurement, a price, a date — is NOT in
   RETRIEVED DATA: NEVER promise to check and come back. "Let me pull", "I'll get back to
   you", "someone will follow up" are BANNED — the human reading this draft IS the person
   who would do that, so hand them a draft they can finish, not an errand. Write the answer
   sentence anyway with ______ (six underscores) where the missing fact belongs:
   "The inseam on the Sagan in a 27 is ______." Then set flag_for_review to true.
   Decorative template placeholders you can't fill (not facts she asked for): restructure
   the sentence so they aren't needed.
2b. If the scenario needs her order number and she didn't include one: the answer IS
   asking for the order number (one question), plus whatever you can answer without it.
   Do not guess at order state you cannot see.
3. Close with exactly: Warmly, Eva
3b. If earlier turns are shown, you are continuing a conversation, not starting one. Pick up
   where it left off — no reintroducing yourself, no repeating what she was already told.
4. Never tell her to contact the carrier herself. We own it.
5. Never promise a refund amount, a fee waiver, a discount code, expedited shipping, or
   messenger delivery.
6. At most one exclamation point in the entire draft. Never use an em dash or en dash —
   write two sentences or use a comma instead.
6a. If tracking_url or tracking_number is in RETRIEVED DATA, give it as ONE compact line:
   "Tracking {number}: {url}". Never write that tracking "should be in your inbox".
6c. Plain text only. No markdown: no **bold**, no [text](url) links, no bullet syntax.
   A link is the bare URL on its own.
6b. THE DRAFT ENDS WHEN THE ANSWER ENDS. No trailing reassurance or invitation sentences,
   ever: never "if anything looks off…", "just let me know…", "I'm right here",
   "feel free to…", "don't hesitate…", "happy to help with anything else". Never suggest
   visiting the showroom unprompted. Warmth lives in HOW you answer, not in an extra
   sentence after it. The only sentence permitted after the answer is a direct question
   for information you genuinely need ("What size are you looking for?"). Then the signoff.
7. Never write "Dear Valued Customer". Never open with "unfortunately" unless the answer is
   a hard no.

Set flag_for_review to true if the template did not actually cover what she asked, if she
asked more than one question and you could only answer some, or if you had to work around a
missing fact.

Write the email body only — no subject line, no preamble to the human, no commentary."""


SCHEMA = {
    "type": "object",
    "properties": {
        "draft_text": {"type": "string"},
        "flag_for_review": {"type": "boolean"},
    },
    "required": ["draft_text", "flag_for_review"],
    "additionalProperties": False,
}


def build_draft(
    llm: LLM,
    email: Email,
    cls: Classification,
    facts: OrderFacts,
    history: list[dict] | None = None,
    voice_examples: list[str] | None = None,
    policy: str | None = None,
) -> Draft:
    ctx = knowledge.retrieve(cls.category)

    system = cached_system(
        SYSTEM_RULES,
        knowledge.brand_voice(),
        knowledge.lessons(),
        policy or knowledge.policy_digest(),
    )

    first_name = cls.customer_first_name or facts.fields.get("first_name") or ""
    parts = []
    if history:
        turns = []
        for h in history[-6:]:
            who = "SHE WROTE" if h["direction"] == "inbound" else "WE REPLIED"
            turns.append(f"[{who}, {h.get('received_at', '')[:10]}]\n{(h.get('body') or '').strip()[:1500]}")
        parts.append(
            "EARLIER IN THIS CONVERSATION (oldest first) — do not repeat an answer already\n"
            "given, do not contradict a commitment already made, and do not re-explain a\n"
            "policy already explained:\n\n" + "\n\n".join(turns) + "\n"
        )
    parts += [
        f"HER LATEST EMAIL (this is what you are replying to)\nSubject: {email.subject}\n\n{email.body}",
        f"\nMATCHED CATEGORY: {cls.category}",
        f"\nAPPROACH FOR THIS SCENARIO:\n{ctx['approach']}",
    ]
    if ctx["template"]:
        parts.append(f"\nTEMPLATE TO ADAPT (do not send verbatim, fill only from retrieved data):\n{ctx['template']}")
    if voice_examples:
        # Real sent replies, tone reference only. Their facts belong to other customers;
        # copying one is caught by verify because it won't be in the retrieved data below.
        joined = "\n\n---\n\n".join(voice_examples)
        parts.append(
            "\nREAL PAST REPLIES FROM OUR TEAM (match this tone, rhythm, and warmth. Every "
            "fact in them — names, orders, products, dates, availability — is about a "
            "DIFFERENT customer and is dead to you; reuse the voice, never the facts):\n\n"
            + joined
        )
    parts.append(
        "\nRETRIEVED DATA (the complete set of facts you may state — order facts, garment "
        "measurements, prices; anything not here does not exist for you):\n"
        + (json.dumps(facts.fields, indent=2) if facts.fields else "(none — this category needs no lookup)")
    )
    if first_name:
        parts.append(f"\nHer first name: {first_name}")
    else:
        parts.append("\nHer first name is unknown — open with 'Hi there,' rather than guessing a name.")

    return _parse(
        llm.structured(
            system=system,
            user="\n".join(parts),
            schema=SCHEMA,
            effort="high",
            max_tokens=4096,
        )
    )


def _parse(raw: dict) -> Draft:
    return Draft(text=raw["draft_text"].strip(), flag_for_review=bool(raw["flag_for_review"]))
