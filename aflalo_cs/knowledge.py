"""Loads the knowledge artifacts in knowledge/ and answers retrieval questions."""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"


@functools.lru_cache(maxsize=1)
def brand_voice() -> str:
    return (KNOWLEDGE_DIR / "brand_voice.md").read_text()


@functools.lru_cache(maxsize=1)
def policy_facts() -> dict[str, Any]:
    return yaml.safe_load((KNOWLEDGE_DIR / "policy_facts.yaml").read_text())


@functools.lru_cache(maxsize=1)
def sizing() -> dict[str, Any]:
    """Finished-garment measurements from Production's spec folder. See SIZING.md."""
    return yaml.safe_load((KNOWLEDGE_DIR / "sizing.yaml").read_text())


def sizing_styles() -> dict[str, Any]:
    """Only the styles cleared for customer-facing use. `withheld` is deliberately not
    reachable from here — a style is withheld because quoting it would be wrong."""
    return sizing()["styles"]


def find_style(text: str | None) -> str | None:
    """Match a style by name in free text, longest name first.

    Longest-first matters: a catalogue will eventually hold both 'Dara' and a 'Dara Midi',
    and the short name would otherwise shadow the long one.
    """
    if not text:
        return None
    lowered = text.lower()
    for key, style in sorted(
        sizing_styles().items(), key=lambda kv: -len(kv[1]["name"])
    ):
        if style["name"].lower() in lowered:
            return key
    return None


def measurements_for(style_key: str, size: str) -> dict[str, float]:
    """Every recorded measurement for one style in one size, in inches.

    Returns {} for an unknown style or a size that style is not made in — an empty result
    is what routes the email to a human, which is the correct outcome for 'we do not have
    that'. Never guess a neighbouring size.
    """
    style = sizing_styles().get(style_key)
    if not style or size not in style["sizes"]:
        return {}
    i = style["sizes"].index(size)
    return {
        point: values[i]
        for point, values in style.get("measurements_in", {}).items()
        if i < len(values)
    }


def may_recommend_a_size() -> bool:
    """Stating a measurement is a fact. Naming a size is a judgement we cannot ground."""
    return bool(sizing()["usage_policy"]["may_recommend_a_size"])


def lessons() -> str:
    """Distilled team-review feedback, binding on every draft.

    Written by `aflalo-cs learn` from the Airtable 'Comments + Feedback' column — the
    standing loop that turns reviewer comments into agent behavior without code changes.
    Not lru-cached: `learn` updates it mid-process and the next draft must see it.
    """
    path = KNOWLEDGE_DIR / "lessons.md"
    return path.read_text() if path.exists() else ""


@functools.lru_cache(maxsize=1)
def _taxonomy() -> dict[str, Any]:
    return yaml.safe_load((KNOWLEDGE_DIR / "categories.yaml").read_text())


def categories() -> dict[str, dict[str, Any]]:
    return _taxonomy()["categories"]


def category_names() -> list[str]:
    return sorted(categories().keys())


def get_category(name: str) -> dict[str, Any]:
    """Unknown categories degrade to `other`, which is a human tier — fail safe."""
    return categories().get(name) or categories()["other"]


def tier(name: str) -> str:
    return get_category(name).get("tier", "human")


def needs_order(name: str) -> bool:
    return bool(get_category(name).get("needs_order", False))


def required_fields(name: str) -> list[str]:
    return list(get_category(name).get("fields", []))


def gate(name: str) -> str | None:
    """For manager-tier categories: 'policy' (commits an exception) or 'action' (a human
    must do the thing the draft claims is done, then send)."""
    return get_category(name).get("gate")


def is_customer_facing(name: str | None) -> bool:
    """False only for the business-mail categories (recruiting, pitches, invoices…).
    Unknown or unclassified defaults to True — a customer wrongly counted is better than a
    customer silently dropped from the SLA numbers."""
    if not name:
        return True
    return bool(get_category(name).get("customer_facing", True))


def is_hard_no(name: str) -> bool:
    """Scenarios where the deck says 'unfortunately' is appropriate."""
    return bool(get_category(name).get("hard_no", False))


def retrieve(category: str) -> dict[str, Any]:
    """The Context Retriever: template + approach for the matched category."""
    cat = get_category(category)
    return {
        "category": category,
        "approach": cat.get("approach", ""),
        "template": cat.get("template"),
        "note": cat.get("note", ""),
    }


def policy_digest() -> str:
    """A compact, stable rendering of policy facts for the drafter prompt.

    The return window and refund fee are stated with the PUBLISHED site policy as source
    (conflicts #1/#2 resolved 2026-08-26 — see CONFLICTS.md). Goodwill exceptions (fee
    waivers, out-of-window credit) stay excluded: the site publishes the rule, not the
    exceptions, and exceptions are manager decisions.
    """
    p = policy_facts()
    lines = [
        "AFLALO policy facts you may rely on (nothing outside this list, plus retrieved order data):",
        f"- Returns are initiated at {p['returns']['channel_clothing']} for clothing; jewelry returns start by {p['returns']['channel_jewelry']}.",
        "- PUBLISHED return policy (aflalonyc.com/policies/refund-policy): domestic returns "
        f"within {p['returns']['window_days']} days of the SHIP date; international within "
        f"{p['returns']['window_international_days']} days of DELIVERY, by email. Items unworn, "
        "original condition and packaging.",
        f"- A refund carries a ${p['returns']['refund_fee_domestic_usd']} fee (${p['returns']['refund_fee_international_usd']} international), "
        "WAIVED entirely when she chooses store credit. Refunds land within "
        f"{p['returns']['refund_processing_days']} business days of us receiving the return.",
        f"- The prepaid return label is emailed after the portal submission ({p['returns']['label']}).",
        "- Out-of-window returns (internal rule): the portal cannot make a label past the "
        f"window. Up to {p['returns']['out_of_window_grace_days']} days past it we accept for "
        "STORE CREDIT (the team makes the label by hand); beyond that we do not allow the "
        "return. State the outcome, never the internal cutoff number.",
        f"- Exchanges: {'offered' if p['exchanges']['offered'] else 'NOT offered'}. {p['exchanges']['guidance']}",
        f"- Final sale items are not returnable and not eligible for store credit.",
        f"- Damaged box must be reported with photos within {p['damage']['box_damaged_report_window_days']} days of delivery.",
        f"- When the error is ours: {p['damage']['our_error_remedy']}.",
        "- We do not have welcome or first-time-buyer discount codes.",
        "- We do not offer custom jewelry.",
        f"- Every email closes with exactly: {p['fixed_signoff']}",
        "",
        "Prices: a price MAY be stated when it appears in RETRIEVED DATA (it comes live from",
        "Shopify), always with the market disclaimer. A price from anywhere else is forbidden.",
        "",
        "You must NOT: promise to waive the return fee, state a refund TOTAL in dollars,",
        "offer store credit outside the published window (a goodwill exception, manager",
        "decision), state any discount code, any price not present in retrieved data, or any",
        "promise of messenger delivery.",
    ]
    return "\n".join(lines)
