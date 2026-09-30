"""Orchestration.

Gmail -> dedup -> hard safety net -> classify -> ROUTE -> Shopify -> retrieve -> draft
      -> verify -> lint -> Gmail draft + label -> log

Error philosophy: any failure partway through a message (API error, unexpected shape, order
not found, ambiguous match, a Verify violation) routes THAT message to needs-human and logs
it. A failure on one message never crashes the run.
"""

from __future__ import annotations

import logging

from . import draft as drafter
from . import knowledge, phishing, router, safety, verify, voice, voice_lint
from .classify import classify
from .gmail_client import Mailbox
from .llm import LLM, LLMError
from . import shopify as shopify_mod
from .models import PHISHING_LABEL, WRITES_DRAFT, Decision, Draft, Email, OrderFacts, Outcome, Routing
from .shopify import Shopify
from .store import Store

log = logging.getLogger("aflalo_cs")

# Categories whose answer may draw on knowledge/sizing.yaml. The measurements enter the
# same grounding container as order facts — one door for every fact source.
SIZING_CATEGORIES = frozenset({"fit_question"})


class Pipeline:
    def __init__(
        self,
        *,
        mailbox: Mailbox,
        llm: LLM,
        shop: Shopify,
        store: Store,
        verify_with_model: bool = True,
        shadow: bool = False,
    ) -> None:
        self.mailbox = mailbox
        self.llm = llm
        self.shop = shop
        self.store = store
        self.verify_with_model = verify_with_model
        self.shadow = shadow
        self._voice: list[voice.Exemplar] | None = None  # cleaned once, first use
        self._policy_cache: str | None = None

    def _policy(self) -> str:
        """Standing policy + the shop-level facts that behave like policy. Where we ship
        is the canonical example: stable, small, and the difference between answering
        'do you ship to Australia?' and asking her where she lives."""
        if self._policy_cache is None:
            base = knowledge.policy_digest()
            try:
                ships = self.shop.ships_to_countries()
            except Exception:  # noqa: BLE001 — policy still stands without the extra line
                ships = []
            if len(ships) >= 100:
                base += (
                    f"\n- Shipping: we ship worldwide ({len(ships)} countries). Answer "
                    "shipping-coverage questions with exactly that and stop. Mention "
                    "duties/taxes (calculated at checkout) only when she asks about them."
                )
            elif ships:
                base += "\n- Shipping: we ship to " + ", ".join(sorted(ships)) + "."
            self._policy_cache = base
        return self._policy_cache

    def _voice_examples(self, email: Email) -> list[str]:
        """The 2-3 most relevant real sent replies — the actual voice, not the deck's
        description of it. Cleaned once per run; safe no-op on an empty corpus."""
        if self._voice is None:
            corpus = self.store.voice_corpus(limit=300) if hasattr(self.store, "voice_corpus") else []
            self._voice = voice.prepare(corpus)
        return voice.top_k(self._voice, email, k=3)

    # ---------------------------------------------------------------- one message

    def process(self, email: Email) -> Outcome:
        try:
            return self._process(email)
        except LLMError as exc:
            return Outcome(
                message_id=email.message_id,
                decision=Decision.NEEDS_HUMAN,
                reason=f"model call failed: {exc}",
                triggered_by="error",
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 — isolate one bad message from the run
            log.exception("unhandled error on %s", email.message_id)
            return Outcome(
                message_id=email.message_id,
                decision=Decision.NEEDS_HUMAN,
                reason=f"unhandled error: {exc}",
                triggered_by="error",
                error=str(exc),
            )

    def _process(self, email: Email) -> Outcome:
        # [2] Hard safety net — before the model is trusted at all.
        hit = safety.check(email.raw_text)
        if hit:
            return Outcome(
                message_id=email.message_id,
                decision=Decision.NEEDS_HUMAN,
                reason=f"safety net: {hit}",
                triggered_by="safety-net",
            )

        # [2b] Impersonation/scam net — deterministic, before the model, and before the
        # business-mail skeletons could courteously reply to a con. A flagged email gets
        # no draft of any kind: even a decline confirms a live inbox.
        prior_senders = []
        if hasattr(self.store, "thread_history") and email.thread_id:
            prior_senders = [
                h["sender"]
                for h in self.store.thread_history(email.thread_id, email.message_id)
                if h.get("direction") == "inbound" and h.get("sender")
            ]
        marks = phishing.assess(email, prior_senders)
        if marks:
            return Outcome(
                message_id=email.message_id,
                decision=Decision.NEEDS_HUMAN,
                reason=phishing.advice(marks),
                triggered_by="phishing-net",
                category="suspected_phishing_or_scam",
                risk="high",
                label_override=PHISHING_LABEL,
            )

        # [3] Classifier — observes only.
        cls = classify(self.llm, email)
        base = Outcome(
            message_id=email.message_id,
            decision=Decision.NEEDS_HUMAN,
            reason="",
            triggered_by="",
            category=cls.category,
            risk=cls.risk,
            signals=cls.signals.as_dict(),
        )

        # [4] Router — deterministic.
        routing = router.route(cls)
        if getattr(email, "followup_owed", False):
            routing = Routing(
                routing.decision,
                f"{routing.reason} | follow-up owed: our last reply promised to come back to her",
                routing.triggered_by,
            )
        if routing.decision is Decision.NEEDS_HUMAN:
            return _finish(base, routing)

        # [5] Shopify lookup — only if this category needs live order data AND she gave an
        # order number. Without one the router already decided: the draft asks for it.
        facts = OrderFacts(found=True)
        if knowledge.needs_order(cls.category) and cls.order_identifier:
            facts = self.shop.lookup(cls.order_identifier, knowledge.required_fields(cls.category))
            routing = router.route_after_lookup(routing, facts)
            base.order_facts = dict(facts.fields)
            if routing.decision is Decision.NEEDS_HUMAN:
                return _finish(base, routing)

        # [5b] Garment measurements — same grounding container as order facts, so verify
        # holds a quoted measurement to the same standard as a tracking number.
        if cls.category in SIZING_CATEGORIES:
            style_key = knowledge.find_style(f"{email.subject} {email.body}")
            if style_key:
                style = knowledge.sizing_styles()[style_key]
                facts.fields["garment_measurements"] = {
                    "style": style["name"],
                    "unit": "inches",
                    "basis": "finished garment measurements, not body measurements",
                    "sizes": {
                        s: knowledge.measurements_for(style_key, s) for s in style["sizes"]
                    },
                }
                routing = Routing(
                    routing.decision,
                    f"{routing.reason} | sizing: {style['name']} spec attached",
                    routing.triggered_by,
                )
            else:
                # The miss list is the ask-Production list — it builds itself in the log.
                routing = Routing(
                    routing.decision,
                    f"{routing.reason} | sizing: no spec on file for the style asked about",
                    routing.triggered_by,
                )
            base.order_facts = dict(facts.fields)

        # [5b2] Catalog facts — whenever she named a piece, the drafter gets what the
        # store knows about it: sizes, per-size stock, price, description. This is the
        # difference between "let me pull that" and an answer.
        if cls.product_mentioned and knowledge.is_customer_facing(cls.category):
            product_facts = self.shop.product_lookup(cls.product_mentioned)
            if product_facts.found:
                for k, v in product_facts.fields.items():
                    facts.fields.setdefault(k, v)
                base.order_facts = dict(facts.fields)

        # [5c] Per-country price — the one number the drafter may quote is retrieved here.
        # She stated no country or no piece? That's still draftable: the honest reply asks
        # for them (the real corpus does exactly this). A failed or all-$0 lookup is not —
        # a human finds the real price rather than a draft guessing one.
        if cls.category == "jewelry_price_inquiry":
            code = shopify_mod.country_code(cls.country)
            if code and cls.product_mentioned:
                price_facts = self.shop.price_lookup(cls.product_mentioned, code)
                if not price_facts.found:
                    return _finish(
                        base,
                        Routing(
                            Decision.NEEDS_HUMAN,
                            f"price lookup failed: {price_facts.error}",
                            "lookup",
                        ),
                    )
                facts.fields.update(price_facts.fields)
                note = f"price: {price_facts.fields.get('product')} for {code} attached"
            else:
                missing = []
                if not cls.product_mentioned:
                    missing.append("piece")
                if not code:
                    missing.append("country" if not cls.country else f"country ({cls.country!r} not recognized)")
                # Team rule (LW): confirm the ship-to country BEFORE providing any quote.
                # The catalog step may have merged a USD price — remove it so the drafter
                # cannot quote it even accidentally.
                for k in ("price_min", "price_max", "price_currency", "price_disclaimer"):
                    facts.fields.pop(k, None)
                base.order_facts = dict(facts.fields)
                note = f"price: no {' or '.join(missing)} stated — draft confirms it first, quotes nothing"
            routing = Routing(routing.decision, f"{routing.reason} | {note}", routing.triggered_by)
            base.order_facts = dict(facts.fields)

        # [6][7] Retrieve + draft.
        history = []
        if hasattr(self.store, "thread_history") and email.thread_id:
            history = self.store.thread_history(email.thread_id, email.message_id)
        d: Draft = drafter.build_draft(
            self.llm, email, cls, facts,
            history=history,
            voice_examples=self._voice_examples(email),
            policy=self._policy(),
        )
        base.draft_text = d.text

        # [8] Verify — either layer failing withholds the draft entirely.
        # Layer 2 gets all three legitimate grounding sources, not just Shopify: reflecting
        # the customer's own report back to her, and stating standing policy, are both fine.
        v = verify.verify(
            self.llm if self.verify_with_model else None,
            d,
            facts,
            # Include the From header: greeting her by her own display name is grounded,
            # and without this line the verifier flags every such greeting as invented.
            customer_email=f"From: {email.sender}\n{email.raw_text}",
            policy=self._policy(),
            action_gated=knowledge.gate(cls.category) == "action",
        )
        # Retrieval annotations ("sizing: no spec on file …") must survive whatever outcome
        # follows — they are the coverage-gap log, and a PARTIAL flag shouldn't erase them.
        notes = routing.reason.split(" | ", 1)[1] if " | " in routing.reason else ""

        def with_notes(reason: str) -> str:
            return f"{reason} | {notes}" if notes else reason

        base.verify = v
        if not v.grounded:
            return _finish(
                base,
                Routing(
                    Decision.DRAFT_UNVERIFIED,
                    with_notes("UNSUPPORTED CLAIM — " + "; ".join(v.all_violations[:3])),
                    "verify",
                ),
            )

        lint = voice_lint.lint(d.text, hard_no=knowledge.is_hard_no(cls.category))
        base.lint_violations = lint
        if lint:
            return _finish(
                base,
                Routing(Decision.DRAFT_UNVERIFIED, with_notes("voice lint: " + "; ".join(lint)), "verify"),
            )

        # A draft with deliberate blanks is COMPLETE except the number — that is a
        # different thing from "partial", and the reviewer should know they're filling
        # in, not rewriting.
        if "___" in d.text:
            return _finish(
                base,
                Routing(
                    Decision.DRAFT_UNVERIFIED,
                    with_notes("FILL-IN — draft is ready except the blanked fact(s); fill in and send"),
                    "verify",
                ),
            )

        if d.flag_for_review:
            flag = "drafter flagged: template did not fully cover the question"
            # A manager-tier draft is already going to a human. Demoting it to needs-human
            # would discard a draft that reviewer wants — keep the queue, note the caveat.
            if routing.decision is Decision.NEEDS_MANAGER_APPROVAL:
                return _finish(
                    base, Routing(routing.decision, f"{routing.reason} | {flag}", routing.triggered_by)
                )
            # The draft is grounded and lint-clean — the model is only saying it couldn't
            # cover everything she asked. That's a partial answer, not a wrong one, so it
            # still goes to the reviewer rather than being thrown away.
            return _finish(
                base, Routing(Decision.DRAFT_UNVERIFIED, with_notes(f"PARTIAL — {flag}"), "verify")
            )

        return _finish(base, routing)

    # ---------------------------------------------------------------- the run

    def run(self, limit: int = 50, reprocess: bool = False) -> list[Outcome]:
        outcomes: list[Outcome] = []
        for email in self.mailbox.fetch_unprocessed(limit):
            # `reprocess` re-runs messages the pipeline has already handled — for when the
            # pipeline itself got smarter. The audit trail keeps every earlier draft
            # (drafts/routing_decisions append; review_rows takes the latest).
            if not reprocess and self.store.already_processed(email.message_id):
                log.info("skipping already-processed %s", email.message_id)
                continue

            outcome = self.process(email)

            if self.shadow:
                # Read-only pass: decide and log, touch nothing. Lets us run against a real
                # inbox and check routing by hand before writing a single draft.
                self.store.record(outcome, thread_id=email.thread_id, email=email)
                outcomes.append(outcome)
                continue

            # [9] Draft Writer — threaded to the original, never sent.
            if outcome.draft_text and outcome.decision in WRITES_DRAFT:
                try:
                    # A category may declare a cc (wholesale loops in Lani) — the draft
                    # carries it so the human sends with the right person already on.
                    cc = knowledge.get_category(outcome.category or "").get("cc")
                    self.mailbox.create_draft(email, outcome.draft_text, cc=cc)
                except Exception as exc:  # noqa: BLE001
                    log.exception("draft write failed for %s", email.message_id)
                    outcome.decision = Decision.NEEDS_HUMAN
                    outcome.reason = f"draft write failed: {exc}"
                    outcome.triggered_by = "error"
                    outcome.error = str(exc)

            try:
                self.mailbox.apply_label(email, outcome.label)
            except Exception as exc:  # noqa: BLE001
                log.exception("label write failed for %s", email.message_id)
                outcome.error = (outcome.error or "") + f" | label write failed: {exc}"

            # [10] Logging — last, so a partial failure is still recorded and dedup holds.
            self.store.record(outcome, thread_id=email.thread_id, email=email)
            outcomes.append(outcome)
        return outcomes


def _finish(base: Outcome, routing: Routing, *, withhold: bool = False) -> Outcome:
    """`withhold` is documentation, not behaviour: a withheld draft stays in the log for the
    audit trail (§10), and `run()` gates on the decision before it ever reaches Gmail."""
    base.decision = routing.decision
    base.reason = routing.reason
    base.triggered_by = routing.triggered_by
    base.withheld = withhold
    return base
