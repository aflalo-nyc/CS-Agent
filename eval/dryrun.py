"""End-to-end run of the full pipeline with a rule-based stand-in for the model.

Everything downstream of the classifier is the real code path: the safety net, the router,
the Shopify lookup, retrieval, both verify layers' deterministic half, the voice lint, the
draft writer and the audit store. Only the model's *judgment* is stubbed, so this shows the
routing behaviour of the real system without needing an API key.

    python -m eval.dryrun
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aflalo_cs import knowledge  # noqa: E402
from aflalo_cs.gmail_client import MockMailbox  # noqa: E402
from aflalo_cs.models import Decision, Signals  # noqa: E402
from aflalo_cs.pipeline import Pipeline  # noqa: E402
from aflalo_cs.shopify import FixtureShopify, normalize_order_number  # noqa: E402
from aflalo_cs.store import Store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

RULES: list[tuple[str, str]] = [
    (r"resize|alter", "alteration_or_resize_request"),
    (r"welcome code|discount code|promo code", "discount_code_request"),
    (r"exchange|too long|too short", "exchange_request"),
    (r"messenger|hand.?deliver", "messenger_delivery_request"),
    (r"delivered cost|duties|what is the (?:total|price)|shows no price", "jewelry_price_inquiry"),
    (r"custom|commission", "custom_jewelry_request"),
    (r"lost in transit|never arrived|package is lost", "lost_in_transit"),
    (r"damaged|tear|broken|scratch", "damaged_item"),
    (r"wrong (?:item|piece|necklace|size)|received a different", "wrong_item_received"),
    (r"final sale", "final_sale_return"),
    (r"past .*window|out of window|too late to return", "out_of_window_return"),
    (r"cancel", "preorder_cancel_not_shipped"),
    (r"upgrade .*(?:overnight|expedited)|expedite", "expedited_shipping_repeat"),
    (r"restock|making more|sold out|waitlist", "out_of_stock_discontinued"),
    (r"between sizes|which (?:size|would you recommend)|sizing|too (?:big|large|small)", "fit_question"),
    (r"preorder|pre-order", "preorder_ship_timing"),
    (r"stuck|hasn'?t moved|delayed|still with", "delayed_package_carrier"),
    (r"where(?:'s| is| my)|tracking|has it shipped|status", "where_is_my_order"),
]

# Placeholders that are NOT Shopify facts — they're commitments or wording choices a human
# (or the real drafter) supplies. Filling these from a fixed safe phrase is legitimate;
# inventing an order fact is not. Surfacing the distinction is the point.
PROSE_DEFAULTS = {
    "followup_time": "the end of the day tomorrow",
    "carrier_location_clause": " due to a transit delay on their end",
    "shipping_speed": "expedited",
}


class RuleBasedLLM:
    """Keyword classifier + template-filling drafter. Deterministic, no API key."""

    def structured(self, *, system, user, schema, effort="medium", max_tokens=4096):
        props = schema.get("properties", {})
        if "signals" in props:
            return self._classify(user)
        if "draft_text" in props:
            return self._draft(user)
        return {"grounded": True, "violations": []}  # layer-2 stub; layer 1 still runs for real

    def _classify(self, user: str) -> dict:
        low = user.lower()
        category = next((c for rx, c in RULES if re.search(rx, low)), "other")
        sig = Signals(
            angry_or_threatening=bool(re.search(r"terrible|disappointed|third time|unacceptable|!!", low)),
            demands_refund_or_cancel=bool(re.search(r"\brefund\b|\bcancel\b", low)),
            non_english=bool(re.search(r"\b(hola|buongiorno|quería|gracias|pedido)\b", low)),
            time_sensitive_deadline=bool(re.search(r"\bby (?:friday|monday|tomorrow)\b|for an event", low)),
            delivery_dispute=bool(re.search(r"never (?:arrived|received)|marked delivered", low)),
            asks_about_money_amount=bool(re.search(r"how much|price|cost|\$", low)),
        )
        name = None
        if m := re.search(r"\n([A-Z][a-z]+)\s*$", user.strip()):
            name = m.group(1)
        return {
            "signals": sig.as_dict(),
            "rationale": f"keyword rules matched {category}",
            "category": category,
            "risk": "medium" if sig.angry_or_threatening else "low",
            "order_identifier": normalize_order_number(user),
            "customer_first_name": name,
        }

    def _draft(self, user: str) -> dict:
        """Fill the template strictly from retrieved data; drop any line whose placeholder
        has no value — the same discipline the real drafter is instructed to follow."""
        import json

        cat = re.search(r"MATCHED CATEGORY: (\w+)", user).group(1)
        block = re.search(r"does not exist for you\):\n(.*?)(?:\n\nHer first name|\nHer first name|\Z)", user, re.S)
        raw = block.group(1).strip() if block else ""
        facts = json.loads(raw) if raw.startswith("{") else {}

        name = m.group(1) if (m := re.search(r"Her first name: (\w+)", user)) else None
        tpl = knowledge.get_category(cat).get("template") or (
            "Hi {first_name},\n\nThank you for reaching out.\n\nWarmly, Eva"
        )

        values: dict[str, str] = {"first_name": name or "there"}
        values.update({k: str(v) for k, v in facts.items() if v is not None})
        values.update({k: v for k, v in PROSE_DEFAULTS.items() if k not in values})
        if facts.get("carrier_location"):
            values["carrier_location_clause"] = f" at {facts['carrier_location']}"

        lines, flagged = [], False
        for line in tpl.splitlines():
            needed = re.findall(r"\{(\w+)\}", line)
            if any(values.get(p) in (None, "") and p not in PROSE_DEFAULTS for p in needed):
                flagged = True  # a real order fact is missing
                continue        # drop the line rather than emit a placeholder or invent a value
            lines.append(line.format(**{p: values.get(p, "") for p in needed}) if needed else line)

        # Collapse the blank lines left behind by any dropped line.
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        return {"draft_text": text, "flag_for_review": flagged}


def main() -> int:
    db = ROOT / "data" / "dryrun.db"
    db.unlink(missing_ok=True)
    mailbox = MockMailbox(path=ROOT / "fixtures" / "mock_inbox.json")
    pipe = Pipeline(
        mailbox=mailbox,
        llm=RuleBasedLLM(),
        shop=FixtureShopify(path=ROOT / "fixtures" / "shopify" / "orders.json"),
        store=Store(db),
        verify_with_model=False,  # layer 1 only — layer 2 needs a real model
    )

    outcomes = pipe.run(limit=100)
    mark = {"draft": "\033[32mDRAFT  \033[0m", "needs-manager-approval": "\033[33mMANAGER\033[0m", "needs-human": "\033[31mHUMAN  \033[0m"}

    print(f"{'id':<6}{'decision':<9}{'category':<30}reason")
    print("-" * 108)
    for o in outcomes:
        print(f"{o.message_id:<6}{mark[o.decision.value]}  {(o.category or '-'):<30}{o.reason[:58]}")

    n = len(outcomes)
    counts = {d.value: sum(1 for o in outcomes if o.decision is d) for d in Decision}
    print("-" * 108)
    print(f"{n} emails — " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    print(f"auto-draft rate {counts['draft'] / n:.0%}, human-touch rate "
          f"{(counts['needs-human'] + counts['needs-manager-approval']) / n:.0%}")
    print(f"\ndrafts actually written to the mock inbox: {len(mailbox.drafts)}")
    print("labels applied:", {k: v[0] for k, v in list(mailbox.labels.items())[:3]}, "...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
