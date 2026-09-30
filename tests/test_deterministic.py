"""Mocked-model unit tests: routing, grounding, voice lint, idempotency, failure handling.

These are fast, free, and let us force conditions on command (an API failure, a hallucinated
draft) that would be impractical to elicit from a real model. Draft *quality* is a different
kind of test and lives in eval/.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from aflalo_cs import knowledge, router, safety, verify, voice_lint
from aflalo_cs.gmail_client import MockMailbox
from aflalo_cs.llm import FakeLLM, LLMError
from aflalo_cs.models import Classification, Decision, Draft, Email, OrderFacts, Signals
from aflalo_cs.pipeline import Pipeline
from aflalo_cs.shopify import FixtureShopify, normalize_order_number
from aflalo_cs.store import Store

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


# ------------------------------------------------------------------ safety net


@pytest.mark.parametrize(
    "text",
    [
        "I am contacting my attorney about this",
        "I'll be filing a chargeback",
        "I am going to dispute the charge with my bank",
        "My lawyer will be in touch",
        "I'm a journalist writing about this",
        "This is fraud",
    ],
)
def test_safety_net_catches_legal_and_chargeback(text):
    assert safety.check(text) is not None


def test_safety_net_ignores_ordinary_email():
    assert safety.check("Where is my order? It was supposed to arrive Tuesday.") is None


def test_safety_net_runs_before_the_model():
    """A legal email must never reach the classifier at all."""
    llm = FakeLLM(responses=[])  # any model call would raise "ran out of scripted responses"
    pipe = _pipeline(llm)
    out = pipe.process(
        Email("x", "x", "a@b.com", "refund", "I am speaking to my attorney about this order.")
    )
    assert out.decision is Decision.NEEDS_HUMAN
    assert out.triggered_by == "safety-net"
    assert llm.calls == []


# ------------------------------------------------------------------ router


def _cls(**kw) -> Classification:
    base = dict(
        signals=Signals(),
        rationale="r",
        category="where_is_my_order",
        risk="low",
        order_identifier="7412",
    )
    base.update(kw)
    return Classification(**base)


def test_router_drafts_a_clean_informational_email():
    assert router.route(_cls()).decision is Decision.DRAFT


@pytest.mark.parametrize(
    "signal",
    [
        "angry_or_threatening",
        "non_english",
        "asks_about_money_amount",
    ],
)
def test_router_escalates_on_every_risk_signal(signal):
    # refund asks, deadlines, and delivery disputes are deliberately absent: they now
    # force a MANAGER-GATED draft instead of nothing (soft gates). Anger on top of any
    # of them still hard-escalates.
    r = router.route(_cls(signals=Signals(**{signal: True})))
    assert r.decision is Decision.NEEDS_HUMAN
    assert r.triggered_by == "signal"


def test_router_escalates_on_high_risk():
    assert router.route(_cls(risk="high")).decision is Decision.NEEDS_HUMAN


def test_a_missing_order_number_drafts_the_ask_instead_of_escalating():
    """'Where is my order' with no order number is not a dead end — the correct reply IS
    asking for the number. The router keeps it draftable and says why."""
    r = router.route(_cls(order_identifier=None))
    assert r.decision is Decision.DRAFT
    assert r.triggered_by == "lookup"
    assert "the draft asks for it" in r.reason


def test_router_sends_expedited_shipping_to_manager_regardless_of_confidence():
    """CS deck: EVERY expedited request needs manager approval. Policy fact, not a
    confidence-calibration problem."""
    for cat in ("expedited_shipping_repeat", "expedited_shipping_new"):
        r = router.route(_cls(category=cat, risk="low"))
        assert r.decision is Decision.NEEDS_MANAGER_APPROVAL, cat


def test_router_never_drafts_a_human_tier_category():
    human = [c for c in knowledge.category_names() if knowledge.tier(c) == "human"]
    assert human, "expected some human-only categories"
    for cat in human:
        r = router.route(_cls(category=cat))
        assert r.decision is Decision.NEEDS_HUMAN, cat


def test_unknown_category_fails_safe():
    """A model that invents a category must not get a draft out of it."""
    assert router.route(_cls(category="totally_made_up")).decision is Decision.NEEDS_HUMAN


def test_lookup_failure_always_wins():
    ok = router.route(_cls())
    assert ok.decision is Decision.DRAFT
    after = router.route_after_lookup(ok, OrderFacts(found=False, error="order not found"))
    assert after.decision is Decision.NEEDS_HUMAN
    assert after.triggered_by == "lookup"


# ------------------------------------------------------------------ grounding, layer 1


FACTS = OrderFacts(
    found=True,
    order_id="7412",
    fields={
        "order_number": "7412",
        "fulfillment_status": "fulfilled",
        "tracking_number": "1Z999AA10123456784",
        "estimated_delivery": "August 12, 2026",
        "item": "Lido Pull-On Pant",
    },
)


def test_layer1_passes_a_grounded_draft():
    d = Draft("Hi Marguerite,\n\nYour order is on its way — tracking 1Z999AA10123456784, "
              "arriving by August 12, 2026.\n\nWarmly, Eva")
    assert verify.layer1(d, FACTS) == []


@pytest.mark.parametrize(
    "text,fragment",
    [
        ("Tracking is 1Z999AA10999999999.", "tracking number"),
        ("Your order #9999 shipped.", "order number"),
        ("It'll arrive by September 3, 2026.", "date"),
        ("Your refund of $240.00 is processed.", "amount"),
        ("The white gold version is on its way.", "product attribute"),
        ("Arriving {estimated_delivery}.", "placeholder"),
        ("Arriving [date].", "placeholder"),
    ],
)
def test_layer1_catches_invented_facts(text, fragment):
    v = verify.layer1(Draft(f"Hi,\n\n{text}\n\nWarmly, Eva"), FACTS)
    assert any(fragment in x for x in v), v


def test_layer1_catches_shipped_claim_on_unshipped_order():
    """The subtle one: nothing is invented, but the state claim contradicts the data."""
    unshipped = OrderFacts(found=True, fields={"fulfillment_status": "unfulfilled", "order_number": "7488"})
    v = verify.layer1(Draft("Hi,\n\nYour order is on its way!\n\nWarmly, Eva"), unshipped)
    assert any("fulfillment_status" in x for x in v), v


def test_layer2_failure_is_not_a_pass():
    """A verifier that couldn't run must not silently approve the draft."""
    llm = FakeLLM(responses=[LLMError("boom")])
    r = verify.verify(llm, Draft("Hi,\n\nAll set.\n\nWarmly, Eva"), FACTS)
    assert r.grounded is False
    assert any("could not run" in x for x in r.layer2_violations)


# ------------------------------------------------------------------ voice lint


def test_lint_requires_the_signoff():
    assert any("sign-off" in v for v in voice_lint.lint("Hi,\n\nAll set.\n\nBest, Eva"))
    assert voice_lint.lint("Hi,\n\nAll set.\n\nWarmly, Eva") == []


@pytest.mark.parametrize(
    "body,fragment",
    [
        ("Dear Valued Customer, all set.", "Dear Valued Customer"),
        ("Please contact the carrier for details.", "carrier"),
        ("Great!! So excited!!", "exclamation"),
        ("I'm sorry. I apologize. We apologize again.", "over-apolog"),
    ],
)
def test_lint_catches_deck_dont_rules(body, fragment):
    v = voice_lint.lint(f"Hi,\n\n{body}\n\nWarmly, Eva")
    assert any(fragment in x for x in v), v


def test_lint_allows_unfortunately_on_a_hard_no():
    text = "Hi,\n\nUnfortunately final sale items aren't eligible for return.\n\nWarmly, Eva"
    assert any("unfortunately" in v for v in voice_lint.lint(text, hard_no=False))
    assert voice_lint.lint(text, hard_no=True) == []


# ------------------------------------------------------------------ shopify


def test_order_number_extraction():
    assert normalize_order_number("order #7412 please") == "7412"
    assert normalize_order_number("Order 7412") == "7412"
    assert normalize_order_number("no number here") is None


def test_fixture_lookup_projects_only_requested_fields():
    shop = FixtureShopify(path=FIXTURES / "shopify" / "orders.json")
    facts = shop.lookup("7412", ["tracking_number", "estimated_delivery"])
    assert facts.found
    assert "tracking_number" in facts.fields
    assert "orders_count" not in facts.fields  # not requested -> drafter never sees it


def test_missing_order_is_not_found():
    shop = FixtureShopify(path=FIXTURES / "shopify" / "orders.json")
    assert shop.lookup("9981", ["tracking_number"]).found is False


# ------------------------------------------------------------------ pipeline


def _pipeline(llm, tmp_db="/tmp/aflalo_test.db", verify_with_model=True):
    Path(tmp_db).unlink(missing_ok=True)
    return Pipeline(
        mailbox=MockMailbox(path=FIXTURES / "mock_inbox.json"),
        llm=llm,
        shop=FixtureShopify(path=FIXTURES / "shopify" / "orders.json"),
        store=Store(tmp_db),
        verify_with_model=verify_with_model,
    )


def _classification_response(**over):
    base = {
        "signals": Signals().as_dict(),
        "rationale": "routine status question",
        "category": "where_is_my_order",
        "risk": "low",
        "order_identifier": "7412",
        "customer_first_name": "Marguerite",
    }
    base.update(over)
    return base


GOOD_DRAFT = (
    "Hi Marguerite,\n\nYour order is on its way. Here's your tracking: "
    "https://www.ups.com/track?tracknum=1Z999AA10123456784\n\n"
    "It's estimated to arrive by August 12, 2026. If anything changes, I'll reach out "
    "directly.\n\nWarmly, Eva"
)


def test_happy_path_produces_a_draft():
    llm = FakeLLM(
        responses=[
            _classification_response(),
            {"draft_text": GOOD_DRAFT, "flag_for_review": False},
            {"grounded": True, "violations": []},
        ]
    )
    out = _pipeline(llm).process(Email("m001", "t001", "m@e.com", "order 7412", "where is it?"))
    assert out.decision is Decision.DRAFT
    assert out.verify.grounded


def test_hallucinated_draft_is_never_labelled_ready_to_send():
    """Layer 1 is independent of the model — a model that rubber-stamps a bad draft must not
    be able to make it look verified. The draft still reaches the human (a warned reviewer
    beats a blank page) but under a label that says a check failed."""
    bad = GOOD_DRAFT.replace("1Z999AA10123456784", "1Z000BB20000000000")
    llm = FakeLLM(
        responses=[
            _classification_response(),
            {"draft_text": bad, "flag_for_review": False},
            {"grounded": True, "violations": []},  # model wrongly approves
        ]
    )
    out = _pipeline(llm).process(Email("m001", "t001", "m@e.com", "order 7412", "where is it?"))
    assert out.decision is Decision.DRAFT_UNVERIFIED
    assert out.decision is not Decision.DRAFT
    assert out.label == "cs/draft-unverified"
    assert "UNSUPPORTED CLAIM" in out.reason
    assert out.draft_text  # the human still gets something to work from


def test_safety_net_never_produces_a_draft():
    """Everything else drafts. Legal/chargeback is the one class where a draft could anchor
    a rushed human toward replying instead of escalating."""
    llm = FakeLLM(responses=[])
    out = _pipeline(llm).process(
        Email("x", "x", "a@b.com", "hi", "I am filing a chargeback and calling my attorney.")
    )
    assert out.decision is Decision.NEEDS_HUMAN
    assert out.draft_text is None
    assert out.label == "cs/no-draft"


def test_shadow_mode_writes_nothing():
    """Design doc §2.10 step 2: run read-only against real mail, label nothing, check the
    routing by hand before writing a single draft."""
    mailbox = MockMailbox(path=FIXTURES / "mock_inbox.json")
    Path("/tmp/aflalo_shadow.db").unlink(missing_ok=True)
    pipe = Pipeline(
        mailbox=mailbox,
        llm=FakeLLM(
            responses=[
                _classification_response(),
                {"draft_text": GOOD_DRAFT, "flag_for_review": False},
                {"grounded": True, "violations": []},
            ]
        ),
        shop=FixtureShopify(path=FIXTURES / "shopify" / "orders.json"),
        store=Store("/tmp/aflalo_shadow.db"),
        shadow=True,
    )
    outcomes = pipe.run(limit=1)
    assert outcomes and outcomes[0].decision is Decision.DRAFT  # it decided
    assert mailbox.drafts == {}  # but wrote nothing
    assert mailbox.labels == {}


def test_api_failure_routes_to_human_not_a_crash():
    llm = FakeLLM(responses=[LLMError("503 overloaded")])
    out = _pipeline(llm).process(Email("m001", "t001", "m@e.com", "order 7412", "where is it?"))
    assert out.decision is Decision.NEEDS_HUMAN
    assert "503" in (out.error or "")


def test_drafter_self_flag_keeps_the_draft():
    """A self-flag means 'I couldn't cover everything she asked' — a partial answer, not a
    wrong one. The draft still reaches the reviewer, labelled so."""
    llm = FakeLLM(
        responses=[
            _classification_response(),
            {"draft_text": GOOD_DRAFT, "flag_for_review": True},
            {"grounded": True, "violations": []},
        ]
    )
    out = _pipeline(llm).process(Email("m001", "t001", "m@e.com", "order 7412", "where is it?"))
    assert out.decision is Decision.DRAFT_UNVERIFIED
    assert "PARTIAL" in out.reason
    assert out.draft_text


def test_idempotency_survives_a_failed_label_write():
    """The real duplicate-draft risk: the draft is created, then the label write fails, so
    the inbox filter still sees the message as unprocessed on the next run. The store's
    message-ID dedup is the independent second net that must catch it."""
    db = "/tmp/aflalo_idem.db"
    Path(db).unlink(missing_ok=True)
    store = Store(db)

    class LabelWriteFails(MockMailbox):
        draft_calls: int = 0

        def apply_label(self, email, label):  # noqa: D102
            raise RuntimeError("Gmail label write failed")

        def create_draft(self, email, body, cc=None):  # noqa: D102
            type(self).draft_calls += 1
            return super().create_draft(email, body, cc=cc)

        def fetch_unprocessed(self, limit=50):  # noqa: D102
            return self._emails[:1]  # same message keeps coming back — nothing got labelled

    mailbox = LabelWriteFails(path=FIXTURES / "mock_inbox.json")

    def build():
        return Pipeline(
            mailbox=mailbox,
            llm=FakeLLM(
                responses=[
                    _classification_response(),
                    {"draft_text": GOOD_DRAFT, "flag_for_review": False},
                    {"grounded": True, "violations": []},
                ]
            ),
            shop=FixtureShopify(path=FIXTURES / "shopify" / "orders.json"),
            store=store,
        )

    build().run(limit=1)
    assert LabelWriteFails.draft_calls == 1
    build().run(limit=1)
    assert LabelWriteFails.draft_calls == 1, "dedup failed — a duplicate draft was created"


def test_never_sends_only_drafts():
    """The Gmail scopes exclude send. Assert the mock surface has no send at all."""
    from aflalo_cs import gmail_client

    assert "https://www.googleapis.com/auth/gmail.send" not in gmail_client.SCOPES
    assert not hasattr(MockMailbox(path=FIXTURES / "mock_inbox.json"), "send")


# ------------------------------------------------------------------ knowledge integrity


def test_every_category_declares_a_tier_and_a_reason_or_template():
    for name, cat in knowledge.categories().items():
        assert cat.get("tier") in {"draft", "manager", "human"}, name
        if cat["tier"] == "human":
            assert cat.get("reason_for_tier"), f"{name} is human-only with no stated reason"
        else:
            assert cat.get("approach"), f"{name} is draftable with no approach"
        if cat["tier"] == "manager":
            assert cat.get("gate") in {"policy", "action"}, f"{name} needs a gate reason"


def test_every_deck_scenario_is_covered():
    """Every inbound scenario in the CS Guide deck must map to a category. The two outbound
    scenarios are explicitly excluded per design doc §2.0."""
    deck_scenarios = {
        "out_of_window_return",
        "final_sale_return",
        "delayed_package_carrier",
        "delayed_package_aflalo",
        "lost_in_transit",
        "wrong_item_received",
        "expedited_shipping_repeat",
        "expedited_shipping_new",
        "damaged_item",
        "where_is_my_order",
        "preorder_ship_timing",
        "order_status_shipping_timeline",
        "cancelled_unable_to_fulfill",
        "preorder_cancel_not_shipped",
        "preorder_cancel_in_transit",
        "fit_question",
        "out_of_stock_restocking",
        "out_of_stock_discontinued",
        "custom_jewelry_request",
    }
    missing = deck_scenarios - set(knowledge.category_names())
    assert not missing, f"deck scenarios with no category: {missing}"

    # Every deck scenario must produce a draft (Tier 1 or 2) — never Tier 3.
    # Tier 3 is reached at runtime via signals, not by scenario.
    not_draftable = {s for s in deck_scenarios if knowledge.tier(s) == "human"}
    assert not not_draftable, f"deck scenarios that produce no draft: {not_draftable}"


def test_no_template_contains_an_amount_or_a_code():
    """Amounts and discount codes are never draftable — not in any tier. Denying store
    credit is fine; naming a figure is not."""
    for name, cat in knowledge.categories().items():
        tpl = cat.get("template")
        if not tpl:
            continue
        assert "$" not in tpl, f"{name} template contains a dollar amount"
        assert not re.search(r"\b[A-Z]{2,}-?\d{1,3}OFF\b", tpl), f"{name} template contains a code"
        assert "full refund" not in tpl.lower(), (
            f"{name} template says 'full refund' — the restocking fee is unresolved "
            "(CONFLICTS.md #2)"
        )
        assert not re.search(r"\b14[- ]day\b", tpl.lower()), (
            f"{name} template states the return window — unresolved (CONFLICTS.md #1)"
        )


def test_tier1_templates_promise_no_money_and_no_action():
    """A Tier 1 auto-drafted reply goes out with no human touching it, so it must not claim
    any money movement or any physical action was taken."""
    claims = [
        "refund has been issued",
        "i've issued",
        "we'd love to offer you store credit",
        "i've added you to the waitlist",
        "label is attached",
        "i've already initiated",
        "i've upgraded your order",
        "i'm sending the correct",
    ]
    for name, cat in knowledge.categories().items():
        if cat.get("tier") != "draft" or not cat.get("template"):
            continue
        low = cat["template"].lower()
        for claim in claims:
            assert claim not in low, f"Tier 1 category {name} claims: {claim!r}"


def test_policy_digest_states_the_published_policy_and_bans_the_exceptions():
    """Conflicts #1/#2 were resolved 2026-08-26 by the site's own published refund policy,
    which is the promise customers agree to. The drafter now receives the window and fee as
    stateable facts; goodwill exceptions (waivers, out-of-window credit) stay banned."""
    digest = knowledge.policy_digest()
    assert "14 days of the SHIP date" in digest
    assert "7 days of DELIVERY" in digest
    assert "$20" in digest and "$50" in digest
    assert "WAIVED entirely when she chooses store credit" in digest
    assert "must NOT" in digest and "waive the return fee" in digest


# ------------------------------------------------------------------ threading


class _Msg:
    """Minimal stand-in for FetchedEmail."""

    def __init__(self, mid, thread, direction, when, body, sender="c@x.com"):
        self.message_id, self.thread_id, self.direction = mid, thread, direction
        self.received_at, self.body, self.sender = when, body, sender
        self.subject, self.to_addr, self.is_unread, self.folder = "Order 7412", "", True, "INBOX"


def _threaded_store(tmp="/tmp/aflalo_threads.db"):
    Path(tmp).unlink(missing_ok=True)
    s = Store(tmp)
    s.save_inbox([
        # Thread A: she wrote, we replied, she wrote again -> AWAITING a reply
        _Msg("a1", "T-A", "inbound", "2026-08-01T10:00:00Z", "Where is my order?"),
        _Msg("a2", "T-A", "outbound", "2026-08-01T11:00:00Z",
             "Thanks for checking in — your order ships Friday and you'll get "
             "tracking as soon as it's on its way.\n\nWarmly, Eva"),
        _Msg("a3", "T-A", "inbound", "2026-08-02T09:00:00Z", "It's Saturday and nothing came."),
        # Thread B: she wrote, we replied -> ALREADY ANSWERED, nothing to draft
        _Msg("b1", "T-B", "inbound", "2026-08-03T10:00:00Z", "Do you ship to Italy?"),
        _Msg("b2", "T-B", "outbound", "2026-08-03T12:00:00Z",
             "We do ship to Italy — duties are included at checkout.\n\nWarmly, Eva"),
        # Thread C: single unanswered message
        _Msg("c1", "T-C", "inbound", "2026-08-04T10:00:00Z", "Is the Lido pant restocking?"),
    ])
    return s


def test_only_unanswered_threads_need_a_draft():
    """A thread we already replied to is not awaiting anything. Drafting per-message rather
    than per-thread is the fastest way to fill the inbox with noise."""
    ids = {e.message_id for e in _threaded_store().inbox()}
    assert ids == {"a3", "c1"}, ids
    assert "b1" not in ids  # answered
    assert "a1" not in ids  # superseded by a3 in the same thread


def test_thread_history_is_ordered_and_excludes_the_current_message():
    hist = _threaded_store().thread_history("T-A", "a3")
    assert [h["direction"] for h in hist] == ["inbound", "outbound"]
    assert "ships Friday" in hist[1]["body"]
    assert all("Saturday" not in h["body"] for h in hist)


def test_drafter_receives_the_prior_turns():
    """The deck says don't explain the policy twice — the drafter can only obey that if it
    can see what was already said."""
    store = _threaded_store()
    llm = FakeLLM(responses=[
        _classification_response(),
        {"draft_text": GOOD_DRAFT, "flag_for_review": False},
        {"grounded": True, "violations": []},
    ])
    from aflalo_cs.gmail_client import StoredMailbox

    pipe = Pipeline(
        mailbox=StoredMailbox(store=store), llm=llm,
        shop=FixtureShopify(path=FIXTURES / "shopify" / "orders.json"), store=store,
    )
    a3 = next(e for e in store.inbox() if e.message_id == "a3")
    pipe.process(a3)

    draft_prompt = llm.calls[1]["user"]
    assert "EARLIER IN THIS CONVERSATION" in draft_prompt
    assert "ships Friday" in draft_prompt   # our previous reply is visible
    assert "WE REPLIED" in draft_prompt


def test_sent_replies_become_the_voice_corpus():
    """Real replies are the gold-standard voice reference the design doc §3.3 asks for."""
    corpus = _threaded_store().voice_corpus()
    assert corpus and all("Warmly, Eva" in c["body"] for c in corpus)


# ------------------------------------------------------------------ SLA KPIs


def _kpi_store(tmp="/tmp/aflalo_kpis.db"):
    """Four threads covering every shape the two KPIs have to survive."""
    Path(tmp).unlink(missing_ok=True)
    s = Store(tmp)
    s.save_inbox([
        # K-A: answered in 1h, then she wrote back -> reopened, so NOT resolved.
        _Msg("ka1", "K-A", "inbound",  "2026-08-01T10:00:00Z", "Where is my order?"),
        _Msg("ka2", "K-A", "outbound", "2026-08-01T11:00:00Z", "Ships Friday.\n\nWarmly, Eva"),
        _Msg("ka3", "K-A", "inbound",  "2026-08-02T09:00:00Z", "Nothing came."),
        # K-B: answered in 2h and closed -> resolved in 2h.
        _Msg("kb1", "K-B", "inbound",  "2026-08-03T10:00:00Z", "Do you ship to Italy?"),
        _Msg("kb2", "K-B", "outbound", "2026-08-03T12:00:00Z", "We do.\n\nWarmly, Eva"),
        # K-C: nobody has replied at all.
        _Msg("kc1", "K-C", "inbound",  "2026-08-04T10:00:00Z", "Is the Lido pant restocking?"),
        # K-D: WE started the thread; her first message is the one the clock runs from.
        _Msg("kd0", "K-D", "outbound", "2026-08-05T08:00:00Z", "Your piece is back.\n\nWarmly, Eva"),
        _Msg("kd1", "K-D", "inbound",  "2026-08-05T09:00:00Z", "Can you hold it?"),
        _Msg("kd2", "K-D", "outbound", "2026-08-05T12:00:00Z", "Held for you.\n\nWarmly, Eva"),
    ])
    return s


def _kpi(store, thread_id):
    return next(k for k in store.thread_kpis() if k["thread_id"] == thread_id)


def test_first_response_time_measures_her_first_message_to_our_first_reply():
    store = _kpi_store()
    assert _kpi(store, "K-A")["first_response_hours"] == 1.0
    assert _kpi(store, "K-B")["first_response_hours"] == 2.0


def test_an_unanswered_thread_has_no_response_time_rather_than_a_zero():
    """A zero here would report the exact opposite of what happened, and it would drag
    every average toward 'instant'. Null is the honest value; the backlog count is the
    number that should move."""
    k = _kpi(_kpi_store(), "K-C")
    assert k["first_response_hours"] is None
    assert k["resolution_hours"] is None
    assert k["thread_status"] == "Awaiting first reply"
    assert k["open_hours"] > 0


def test_an_outbound_started_thread_does_not_produce_a_negative_response_time():
    """We emailed her first. The response clock starts at HER first message, not at ours,
    or proactive outreach silently reports as sub-zero response times."""
    k = _kpi(_kpi_store(), "K-D")
    assert k["first_response_hours"] == 3.0
    assert k["resolution_hours"] == 3.0


def test_resolution_time_only_counts_threads_where_our_reply_was_the_last_word():
    store = _kpi_store()
    assert _kpi(store, "K-B")["resolution_hours"] == 2.0
    assert _kpi(store, "K-B")["thread_status"] == "Resolved — answered"
    # She wrote back after our reply — still open, and the clock is still running.
    assert _kpi(store, "K-A")["resolution_hours"] is None
    assert _kpi(store, "K-A")["thread_status"] == "Open"


def test_kpi_summary_reports_the_backlog_alongside_the_averages():
    k = _kpi_store().kpi_summary()
    assert k["threads"] == 4
    assert (k["answered"], k["resolved"], k["awaiting_first_reply"]) == (3, 2, 1)
    assert k["first_response_median_h"] == 2.0     # 1, 2, 3
    assert k["resolution_median_h"] == 2.5         # 2, 3
    assert k["oldest_unanswered_h"] > 0


def test_review_rows_carry_the_thread_kpis_to_airtable():
    """The reviewer sees one row per email; the KPIs are a property of the conversation.
    If they don't ride along on the row, they don't reach Airtable at all."""
    from aflalo_cs.airtable import _row_from

    store = _kpi_store()
    kb1 = next(e for e in store.inbox() if e.message_id == "kc1")
    store.record(
        __import__("aflalo_cs.models", fromlist=["Outcome"]).Outcome(
            message_id="kc1", decision=Decision.NEEDS_HUMAN, reason="no policy",
            triggered_by="category", category="other", risk="low", signals={},
        ),
        thread_id=kb1.thread_id,
    )
    row = next(r for r in store.review_rows() if r["message_id"] == "kc1")
    assert row["thread_id"] == "K-C"
    assert row["thread_status"] == "Awaiting first reply"
    assert row["first_response_hours"] is None

    fields = _row_from(row)
    assert fields["Thread Status"] == "Awaiting first reply"
    assert fields["First Response (hrs)"] is None
    assert fields["Thread Msgs"] == 1


def test_thread_rows_reach_airtable_with_nulls_intact():
    """CS Threads is what any SLA rollup points at. If an unanswered thread arrives there
    as a 0 instead of a blank, the average response time reports the opposite of reality."""
    from aflalo_cs.airtable import _thread_row_from

    rows = {r["thread_id"]: _thread_row_from(r) for r in _kpi_store().thread_kpis()}
    assert rows["K-C"]["First Response (hrs)"] is None
    assert rows["K-C"]["Thread Status"] == "Awaiting first reply"
    assert rows["K-C"]["Open (hrs)"] > 0
    assert rows["K-B"]["First Response (hrs)"] == 2.0
    assert rows["K-B"]["Resolution (hrs)"] == 2.0
    assert rows["K-B"]["Open (hrs)"] is None
    assert rows["K-A"]["Resolution (hrs)"] is None      # she wrote back — still open


# ------------------------------------------------------------------ Shopify auth


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code):
    import io
    import urllib.error

    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(b"denied"))


def _fake_transport(monkeypatch, script):
    """Stand in for urlopen. `script` maps a URL fragment to a queue of responses or
    exceptions; the last entry repeats once the queue runs down. Returns the call log."""
    from aflalo_cs import shopify as shopify_mod

    calls = []

    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        for fragment, queue in script.items():
            if fragment in req.full_url:
                item = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(item, Exception):
                    raise item
                return _FakeResponse(item)
        raise AssertionError(f"unexpected request to {req.full_url}")

    monkeypatch.setattr(shopify_mod.urllib.request, "urlopen", urlopen)
    return calls


TOKEN_OK = {"access_token": "shpat_minted", "scope": "read_orders", "expires_in": 86399}
ORDER_OK = {"data": {"orders": {"edges": [{"node": {
    "name": "#7412", "displayFulfillmentStatus": "FULFILLED",
    "customer": {"firstName": "Ana"}, "lineItems": {"edges": []}, "fulfillments": [],
}}]}}}

TOKEN_URL, GRAPHQL_URL = "/admin/oauth/access_token", "/graphql.json"


def _live_client(**kw):
    from aflalo_cs.shopify import ShopifyAdminClient

    return ShopifyAdminClient(shop_domain="aflalo.myshopify.com", **kw)


def _mints(calls):
    return sum(TOKEN_URL in c for c in calls)


def test_client_credentials_are_exchanged_for_a_token_once_and_reused(monkeypatch):
    """What we have from Shopify is a client id and secret, not an access token. The
    exchange is what turns them into one — and it must happen once, not per lookup."""
    calls = _fake_transport(monkeypatch, {TOKEN_URL: [TOKEN_OK], GRAPHQL_URL: [ORDER_OK]})
    shop = _live_client(client_id="cid", client_secret="shpss_secret")
    assert shop.lookup("7412", ["fulfillment_status"]).found
    assert shop.lookup("7412", ["fulfillment_status"]).found
    assert _mints(calls) == 1
    assert sum(GRAPHQL_URL in c for c in calls) == 2


def test_a_token_past_its_expiry_is_re_minted(monkeypatch):
    """Tokens from this grant last 24 hours, so a long-running process cannot mint once
    at startup and assume it holds."""
    calls = _fake_transport(monkeypatch, {TOKEN_URL: [TOKEN_OK], GRAPHQL_URL: [ORDER_OK]})
    shop = _live_client(client_id="cid", client_secret="shpss_secret")
    shop.lookup("7412", ["fulfillment_status"])
    shop._expires_at = 0.0                       # as if 24 hours had passed
    shop.lookup("7412", ["fulfillment_status"])
    assert _mints(calls) == 2


def test_a_401_re_mints_the_token_and_retries_once(monkeypatch):
    """A revoked or rotated secret reads as a 401 on the API call, not on the exchange."""
    calls = _fake_transport(monkeypatch, {
        TOKEN_URL: [TOKEN_OK, TOKEN_OK],
        GRAPHQL_URL: [_http_error(401), ORDER_OK],
    })
    shop = _live_client(client_id="cid", client_secret="shpss_secret")
    assert shop.lookup("7412", ["fulfillment_status"]).found
    assert _mints(calls) == 2


def test_a_persistent_401_is_reported_rather_than_retried_forever(monkeypatch):
    """Retrying past the first re-mint just hammers the token endpoint with credentials
    that are not going to start working."""
    calls = _fake_transport(monkeypatch, {
        TOKEN_URL: [TOKEN_OK],
        GRAPHQL_URL: [_http_error(401)],
    })
    facts = _live_client(client_id="cid", client_secret="shpss_secret").lookup("7412", ["item"])
    assert not facts.found and "401" in facts.error
    assert _mints(calls) == 2                    # the initial mint plus one retry mint


def test_a_non_auth_error_does_not_trigger_a_re_mint(monkeypatch):
    """A 500 is Shopify having a bad day. Re-minting the token cannot help and the retry
    would double the load on an already-struggling endpoint."""
    calls = _fake_transport(monkeypatch, {
        TOKEN_URL: [TOKEN_OK],
        GRAPHQL_URL: [_http_error(500)],
    })
    facts = _live_client(client_id="cid", client_secret="shpss_secret").lookup("7412", ["item"])
    assert not facts.found and "500" in facts.error
    assert _mints(calls) == 1


def test_a_static_admin_token_is_never_exchanged(monkeypatch):
    """A shpat_ token from an admin-created custom app already IS an access token. Posting
    it to the OAuth endpoint would be a pointless round trip and a 400."""
    calls = _fake_transport(monkeypatch, {GRAPHQL_URL: [ORDER_OK]})
    assert _live_client(access_token="shpat_static").lookup("7412", ["item"]).found
    assert _mints(calls) == 0


def test_a_failed_exchange_escalates_the_email_instead_of_crashing_the_run(monkeypatch):
    """A wrong secret must route that email to a human, not take down the whole run."""
    _fake_transport(monkeypatch, {TOKEN_URL: [_http_error(401)]})
    facts = _live_client(client_id="cid", client_secret="wrong").lookup("7412", ["item"])
    assert not facts.found and "token exchange" in facts.error


def test_a_client_needs_one_of_the_two_credential_shapes():
    from aflalo_cs.shopify import ShopifyAdminClient

    with pytest.raises(ValueError):
        ShopifyAdminClient(shop_domain="aflalo.myshopify.com")


# ------------------------------------------------------------------ config wiring


def test_dotenv_never_overrides_the_real_environment(monkeypatch, tmp_path):
    """`.env` is the convenience; an explicit env var is the override. If the file won,
    a one-off `SHOPIFY_SHOP_DOMAIN=... python -m ...` would silently do nothing."""
    from aflalo_cs import config

    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n\nSHOPIFY_SHOP_DOMAIN=from-file.myshopify.com\n"
        "SHOPIFY_ADMIN_TOKEN=\n"
        'SHOPIFY_API_KEY="quoted-id"\n'
    )
    monkeypatch.setenv("SHOPIFY_SHOP_DOMAIN", "from-env.myshopify.com")
    monkeypatch.delenv("SHOPIFY_API_KEY", raising=False)
    monkeypatch.delenv("SHOPIFY_ADMIN_TOKEN", raising=False)

    loaded = config.load_dotenv(env_file)
    assert os.environ["SHOPIFY_SHOP_DOMAIN"] == "from-env.myshopify.com"  # env wins
    assert os.environ["SHOPIFY_API_KEY"] == "quoted-id"                   # quotes stripped
    assert "SHOPIFY_ADMIN_TOKEN" not in os.environ                        # blank != configured
    assert set(loaded) == {"SHOPIFY_API_KEY"}


def test_get_shopify_picks_the_credential_path_that_is_actually_present(monkeypatch):
    from aflalo_cs import config

    for var in ("SHOPIFY_SHOP_DOMAIN", "SHOPIFY_ADMIN_TOKEN", "SHOPIFY_API_KEY", "SHOPIFY_API_SECRET"):
        monkeypatch.delenv(var, raising=False)
    assert config.get_shopify()[1] == "fixtures"

    # Client id + secret with no domain is not enough — the domain is the host we call.
    monkeypatch.setenv("SHOPIFY_API_KEY", "cid")
    monkeypatch.setenv("SHOPIFY_API_SECRET", "shpss_x")
    assert config.get_shopify()[1] == "fixtures"
    assert "SHOPIFY_SHOP_DOMAIN" in config.shopify_hint()

    monkeypatch.setenv("SHOPIFY_SHOP_DOMAIN", "aflalo.myshopify.com")
    assert config.get_shopify()[1] == "live (client credentials)"

    # An explicit access token wins — no reason to exchange when we already have one.
    monkeypatch.setenv("SHOPIFY_ADMIN_TOKEN", "shpat_static")
    assert config.get_shopify()[1] == "live (admin token)"


def test_an_oauth_error_page_is_reduced_to_the_line_worth_acting_on():
    """Shopify answers a failed exchange with a full HTML page. Unreduced, it lands in
    routing_decisions.reason and makes the audit trail unreadable."""
    from aflalo_cs.shopify import _explain_oauth_error

    page = "<!DOCTYPE html><html><head><title>400 - Oauth error app_not_installed</title>"
    assert "not installed on this store" in _explain_oauth_error(page)
    assert "Install app" in _explain_oauth_error(page)
    assert "client id" in _explain_oauth_error("<title>400 - Oauth error application_cannot_be_found</title>")
    assert "client secret" in _explain_oauth_error("<title>400 - Oauth error invalid_request</title>")
    # An unrecognised shape still yields something, just not a suggestion.
    assert _explain_oauth_error("something else entirely") == "something else entirely"


def test_an_access_denial_names_the_missing_scope_not_the_query():
    """A scope problem and a broken query are different failures. Reported as a raw GraphQL
    error blob, the first looks like the second and someone goes debugging the query."""
    from aflalo_cs.shopify import _explain_graphql_errors

    msg = _explain_graphql_errors([{"message": "Access denied for orders field."}])
    assert "read_orders" in msg and "reinstall" in msg
    assert "protected customer data" in msg          # the step people miss
    other = _explain_graphql_errors([{"message": "Field 'nope' doesn't exist"}])
    assert "GraphQL error" in other and "reinstall" not in other


def test_probe_reports_an_installed_app_with_no_scopes(monkeypatch):
    """The exact state a fresh install lands in: valid token, zero scopes. If probe called
    that healthy, the failure would surface later on a real customer's email instead."""
    calls = _fake_transport(monkeypatch, {
        TOKEN_URL: [TOKEN_OK],
        GRAPHQL_URL: [{"data": {"currentAppInstallation": {"accessScopes": []},
                                "shop": {"name": "AFLALO", "currencyCode": "USD"}}}],
    })
    out = _live_client(client_id="cid", client_secret="shpss_secret").probe()
    assert "NO scopes" in out and "AFLALO" in out
    assert _mints(calls) == 1


def test_probe_names_which_scopes_are_missing(monkeypatch):
    _fake_transport(monkeypatch, {
        TOKEN_URL: [TOKEN_OK],
        GRAPHQL_URL: [{"data": {
            "currentAppInstallation": {"accessScopes": [{"handle": "read_orders"}]},
            "shop": {"name": "AFLALO", "currencyCode": "USD"}}}],
    })
    out = _live_client(client_id="cid", client_secret="shpss_secret").probe()
    assert "read_customers" in out and "read_inventory" in out
    assert "read_orders" not in out.split("missing scopes:")[1]


def test_probe_is_happy_only_when_every_required_scope_is_present(monkeypatch):
    _fake_transport(monkeypatch, {
        TOKEN_URL: [TOKEN_OK],
        GRAPHQL_URL: [{"data": {
            "currentAppInstallation": {"accessScopes": [
                {"handle": "read_orders"}, {"handle": "read_customers"},
                {"handle": "read_inventory"}, {"handle": "read_products"}]},
            "shop": {"name": "AFLALO", "currencyCode": "USD"}}}],
    })
    out = _live_client(client_id="cid", client_secret="shpss_secret").probe()
    assert "all required ones present" in out


def test_an_order_lookup_survives_a_scopeless_install(monkeypatch):
    """This must escalate the email to a human with a readable reason, not crash the run."""
    _fake_transport(monkeypatch, {
        TOKEN_URL: [TOKEN_OK],
        GRAPHQL_URL: [{"errors": [{"message": "Access denied for orders field."}]}],
    })
    facts = _live_client(client_id="cid", client_secret="shpss_secret").lookup("7412", ["item"])
    assert not facts.found
    assert "read_orders" in facts.error


def test_orders_count_is_a_number_even_though_shopify_sends_a_string():
    """numberOfOrders is UnsignedInt64, which GraphQL serialises as "4". Passed through
    untouched it reaches the drafter as text and any numeric comparison silently misreads."""
    from aflalo_cs.shopify import _project

    node = {"customer": {"firstName": "Tegan", "numberOfOrders": "4"}, "lineItems": {"edges": []}}
    assert _project(node, ["orders_count"])["orders_count"] == 4

    # A customer with no orders record must not become 0 — absent and zero differ.
    node["customer"] = {"firstName": "Tegan"}
    assert "orders_count" not in _project(node, ["orders_count"])


def test_the_fixture_shape_matches_what_the_live_api_returns():
    """The fixtures kept the old `ordersCount` name long after Shopify renamed it, which is
    exactly why the broken query survived to production. A fixture that mirrors a shape the
    API no longer has is worse than no fixture."""
    orders = json.loads((FIXTURES / "shopify" / "orders.json").read_text())
    customers = [o["customer"] for o in orders if o.get("customer")]
    assert customers, "fixture has no customers to check"
    for c in customers:
        assert "ordersCount" not in c, "stale field name — live API uses numberOfOrders"
        assert isinstance(c["numberOfOrders"], str), "live API returns this as a string"


def test_probe_names_the_app_it_authenticated_as(monkeypatch):
    """Two similarly-named apps on one store is a real failure mode: you grant scopes to one
    and authenticate as the other, and the symptom is 'nothing I do has any effect'."""
    _fake_transport(monkeypatch, {
        TOKEN_URL: [TOKEN_OK],
        GRAPHQL_URL: [{"data": {
            "currentAppInstallation": {"app": {"title": "sanskriti-cs"}, "accessScopes": []},
            "shop": {"name": "AFLALO", "currencyCode": "USD"}}}],
    })
    out = _live_client(client_id="cid", client_secret="shpss_secret").probe()
    assert "sanskriti-cs" in out and "check the name" in out


def test_a_login_refusal_explains_the_fix_rather_than_quoting_the_server():
    """'Application-specific password required' is the first thing anyone pointing this at
    a real mailbox will hit, and the raw IMAP error is a traceback ending in a URL."""
    from aflalo_cs.inbox_import import _explain_login_failure

    got = _explain_login_failure(
        "[ALERT] Application-specific password required: https://support.google.com/... (Failure)"
    )
    assert "App Password" in got and "myaccount.google.com/apppasswords" in got
    assert "16 lowercase" in got

    assert "IMAP is switched off" in _explain_login_failure("[ALERT] IMAP access is disabled")
    assert "not the account password" in _explain_login_failure("Invalid credentials (Failure)")
    # Anything unrecognised is passed through rather than swallowed.
    assert _explain_login_failure("weird server mood") == "weird server mood"


def test_store_order_notifications_are_dropped_from_both_folders():
    """Shopify mails order notifications from the store's own address to the shared inbox,
    so each one appears in INBOX and in Sent. Against 30 days of the real mailbox they were
    276 of 490 conversations and 276 of 399 messages in the voice corpus — the drafter would
    have learned its tone mostly from a Shopify template."""
    from aflalo_cs.inbox_import import NOISE_SUBJECTS

    assert NOISE_SUBJECTS.match("[AFLALO] Order #7447 placed by Rebecca Strand")
    assert NOISE_SUBJECTS.match("  [aflalo] order 7447 placed by ")

    # Real CS mail from the same address must survive. A reply with no inbound half is a
    # thread whose customer message predates the import window, not a notification — and
    # it is exactly the human writing the voice corpus exists to collect.
    for keep in ("Re: Sizing question", "Re: Necklace Price Inquiry",
                 "Re: Order #7373", "Return requested for order #7412"):
        assert not NOISE_SUBJECTS.match(keep), keep


# ------------------------------------------------------------------ sizing knowledge


def test_sizing_measurements_grow_monotonically_with_size():
    """A transcription slip in a hand-entered spec table is invisible by eye and would be
    quoted to a customer as fact. Every point of measure on every style must increase (or
    hold) as the size goes up — one transposed digit breaks that."""
    from aflalo_cs import knowledge

    for key, style in knowledge.sizing_styles().items():
        for point, values in style.get("measurements_in", {}).items():
            assert values == sorted(values), f"{key}.{point} is not monotonic: {values}"
            assert len(values) == len(style["sizes"]), f"{key}.{point} size-count mismatch"


def test_every_sizing_style_declares_its_units_and_provenance():
    from aflalo_cs import knowledge

    for key, style in knowledge.sizing_styles().items():
        assert style.get("source"), f"{key} has no source file"
        assert style.get("native_unit") in ("in", "cm"), key
        assert style.get("sizes"), key
        if style["native_unit"] == "cm":
            assert style.get("measurements_cm"), f"{key} converted from cm but lost the original"


def test_measurements_for_refuses_a_size_the_style_is_not_made_in():
    """Returning a neighbouring size would be a plausible, checkable-looking, wrong number.
    Empty is what sends the email to a human, which is correct."""
    from aflalo_cs import knowledge

    assert knowledge.measurements_for("aire_dress", "4")["chest_circ"] == 28.5
    assert knowledge.measurements_for("aire_dress", "XL") == {}
    assert knowledge.measurements_for("aire_dress", "3") == {}
    assert knowledge.measurements_for("no_such_style", "4") == {}


def test_withheld_styles_are_unreachable_from_the_customer_facing_accessor():
    """Aksel Pant has grading deltas and no absolute measurements; the bomber file's name
    and contents name two different garments. Quoting either would be confidently wrong."""
    from aflalo_cs import knowledge

    styles = knowledge.sizing_styles()
    for withheld in ("aksel_pant", "audra_jacket", "valeo_bomber"):
        assert withheld not in styles
        assert knowledge.measurements_for(withheld, "M") == {}
        assert knowledge.sizing()["withheld"][withheld]["reason"]


def test_the_drafter_is_not_allowed_to_recommend_a_size():
    """Quoting a measurement is grounded. Naming a size needs her body measurements and an
    ease judgement we do not have, and a wrong one costs a return."""
    from aflalo_cs import knowledge

    assert knowledge.may_recommend_a_size() is False


def test_a_style_is_found_by_name_in_a_customer_sentence():
    from aflalo_cs import knowledge

    assert knowledge.find_style("hi! is the Aire Dress true to size?") == "aire_dress"
    assert knowledge.find_style("what are the measurements on the tibet top") == "tibet_top"
    assert knowledge.find_style("do you have this in a medium") is None


def test_every_entrypoint_loads_dotenv_before_reading_credentials():
    """`.env` is loaded as a side effect of importing config. Any entrypoint that reads
    os.environ before that import sees nothing, and reports 'no credentials' to someone
    whose .env is correctly filled in — which is what happened to the Airtable push."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "aflalo_cs"
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "main"]:
            first_env, first_cfg = None, None
            for i, node in enumerate(ast.walk(fn)):
                pass
            order = list(ast.walk(fn))
            for node in order:
                if (first_env is None and isinstance(node, ast.Attribute)
                        and node.attr == "environ"):
                    first_env = order.index(node)
                if first_cfg is None and isinstance(node, ast.ImportFrom):
                    if any(a.name == "config" for a in node.names):
                        first_cfg = order.index(node)
            if first_env is not None and first_cfg is not None:
                assert first_cfg < first_env, (
                    f"{path.name}:main reads os.environ before importing config, "
                    "so .env will not have been loaded"
                )


def test_the_live_gmail_path_can_never_mark_a_message_read():
    """A draft must appear in the UI with the email still bold and unread. Gmail marks a
    message read only when the UNREAD label is removed, so the guarantee is: nothing in this
    module removes a label. Asserted against the source because it is a property of the whole
    module, not of one function — a future 'mark as handled' helper would break it silently."""
    from pathlib import Path

    from aflalo_cs.gmail_client import SCOPES

    src = (Path(__file__).resolve().parent.parent / "aflalo_cs" / "gmail_client.py").read_text()
    assert "removeLabelIds" not in src, "something removes a Gmail label — that can mark mail read"
    assert "addLabelIds" in src
    # And no send scope, so a bug cannot email a customer. Assert on the granted scopes,
    # not the source text — the module mentions gmail.send in a comment saying it is excluded.
    assert not any(s.endswith("gmail.send") for s in SCOPES), SCOPES
    assert any(s.endswith("gmail.compose") for s in SCOPES)
    assert any(s.endswith("gmail.readonly") for s in SCOPES)


def test_a_live_draft_is_attached_to_the_original_thread():
    """A draft with no threadId becomes a detached new message — the reviewer would not find
    it under the customer's email, which is where they will look."""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "aflalo_cs" / "gmail_client.py").read_text()
    body = src[src.index("def create_draft(self, email: Email, body: str, cc: str | None = None) -> str:"):]
    assert '"threadId": email.thread_id' in body


# ------------------------------------------------------------------ real voice exemplars


def test_the_cleaner_keeps_a_real_reply_and_strips_the_footer():
    from aflalo_cs.voice import clean_reply

    body = (
        "Hi Kelly,\r\n\r\nThank you for reaching out. We will be restocking the Tavi pant in "
        "camel, though we don't have an exact date just yet. What size are you looking for?"
        "\r\n\r\nBest regards,\r\nEva\r\n\r\n<http://www.aflalonyc.com>\r\n56 Greene Street, "
        "Floor 3\r\n<https://www.google.com/maps/place/AFLALO/@40.72>\r\nNew York, NY 10012"
    )
    got = clean_reply(body)
    assert got and "Tavi pant" in got and "Best regards" in got
    assert "aflalonyc.com" not in got and "Greene Street" not in got


def test_the_cleaner_cuts_quoted_history_gmail_style():
    """The import's stripper misses Gmail's wrapped 'On <date> ... wrote:' when the address
    breaks onto the next line. The exemplar must not carry the customer's quoted email."""
    from aflalo_cs.voice import clean_reply

    body = (
        "Hello Jamie,\r\n\r\nUPS cannot locate the package and we are opening an "
        "investigation.\r\n\r\nBest,\r\nEva\r\n\r\nOn Thu, Aug 20, 2026 at 3:51 PM "
        "Jamie Lenore <\r\njamie@x.com> wrote:\r\n> where is my order"
    )
    got = clean_reply(body)
    assert got and "investigation" in got
    assert "jamie@x.com" not in got and "where is my order" not in got


def test_the_cleaner_rejects_what_is_not_a_customer_reply():
    """Footer-only bodies, internal notes without a signoff, and fragments are not the
    voice — an exemplar teaches the model whatever it contains."""
    from aflalo_cs.voice import clean_reply

    assert clean_reply("<http://www.aflalonyc.com>\r\n56 Greene Street, Floor 3") is None
    assert clean_reply(
        "Hi- it looks like you signed for this On friday. were these returned?\r\n\r\n-Lillian"
    ) is None
    assert clean_reply("Hello,\r\nok\r\nBest,\r\nEva") is None  # no real prose


def test_exemplar_selection_prefers_topically_similar_replies():
    from aflalo_cs.models import Email
    from aflalo_cs.voice import prepare, top_k

    corpus = [
        {"subject": "Re: Necklace Price Inquiry",
         "body": "Hi Graças,\r\n\r\nThanks for reaching out. Could you please let me know "
                 "which necklace style you are inquiring about? Please confirm your ship-to "
                 "country.\r\n\r\nBest,\r\nEva"},
        {"subject": "Re: Order 7007",
         "body": "Hello Staci,\r\n\r\nI checked, and we currently don't have the shorts in a "
                 "size 10. Please let me know if you want me to share other styles.\r\n\r\n"
                 "Best,\r\nEva"},
    ]
    email = Email(message_id="x", thread_id="t", sender="a@b.com",
                  subject="Necklace price?", body="How much is the pendant necklace? I'm in Italy.")
    picked = top_k(prepare(corpus), email, k=1)
    assert len(picked) == 1 and "necklace style" in picked[0]


def test_the_drafter_prompt_carries_exemplars_with_the_tone_only_warning():
    from aflalo_cs.draft import build_draft
    from aflalo_cs.models import Classification, Email, OrderFacts, Signals

    llm = FakeLLM(responses=[{"draft_text": GOOD_DRAFT, "flag_for_review": False}])
    build_draft(
        llm,
        Email(message_id="m", thread_id="t", sender="a@b.com", subject="Hi", body="Where is my order #7412?"),
        Classification(signals=Signals(), rationale="", category="order_status_shipping_timeline", risk="low"),
        OrderFacts(found=True, fields={"order_number": "#7412"}),
        voice_examples=["Hi Kelly,\n\nWe will be restocking it.\n\nBest,\nEva"],
    )
    prompt = llm.calls[0]["user"]
    assert "REAL PAST REPLIES FROM OUR TEAM" in prompt
    assert "DIFFERENT customer" in prompt
    assert "restocking" in prompt


# ------------------------------------------------------------------ sizing -> drafter wiring


def _fit_email(body, mid="fit1"):
    return Email(message_id=mid, thread_id=f"T-{mid}", sender="c@x.com",
                 subject="Sizing question", body=body)


def _fit_pipeline(llm, store_path="/tmp/aflalo_fit.db"):
    Path(store_path).unlink(missing_ok=True)
    return Pipeline(
        mailbox=MockMailbox(path=FIXTURES / "mock_inbox.json"), llm=llm,
        shop=FixtureShopify(path=FIXTURES / "shopify" / "orders.json"),
        store=Store(store_path),
    )


def _fit_classification():
    return {
        "signals": {k: False for k in Signals().as_dict()},
        "rationale": "sizing", "category": "fit_question", "risk": "low",
        "order_identifier": None, "customer_first_name": "Ana",
    }


def test_a_known_style_puts_its_measurements_in_front_of_the_drafter():
    llm = FakeLLM(responses=[
        _fit_classification(),
        {"draft_text": "Hi Ana,\n\nThe Aire Dress in a size 4 measures 28.5 inches at the "
                       "chest. These are the garment's finished measurements.\n\nWarmly, Eva",
         "flag_for_review": False},
        {"grounded": True, "violations": []},
    ])
    pipe = _fit_pipeline(llm)
    out = pipe.process(_fit_email("What are the measurements of the Aire Dress in a 4?"))

    draft_prompt = llm.calls[1]["user"]
    assert "garment_measurements" in draft_prompt
    assert "28.5" in draft_prompt                      # the fact was available, verbatim
    assert "finished garment measurements" in draft_prompt
    assert out.decision is Decision.DRAFT
    assert "Aire Dress spec attached" in out.reason


def test_a_quoted_measurement_is_grounded_for_verify_layer1():
    """The whole point of one grounding container: a measurement is checkable the same way
    a tracking number is. Nested dicts must flatten into the haystack."""
    facts = OrderFacts(found=True, fields={
        "garment_measurements": {"style": "Aire Dress", "unit": "inches",
                                 "sizes": {"4": {"chest_circ": 28.5, "waist_circ": 28.125}}},
    })
    assert "28.5" in facts.groundable_values()
    assert "28.125" in facts.groundable_values()


def test_an_unknown_style_is_logged_as_a_missing_spec_not_guessed():
    """Tavi Pant and Sagan jeans are real observed misses. The reply stays honest and the
    routing reason builds the ask-Production list."""
    llm = FakeLLM(responses=[
        _fit_classification(),
        {"draft_text": "Hi Ana,\n\nI'll pull the exact measurements for the Tavi Pant and "
                       "come right back to you.\n\nWarmly, Eva",
         "flag_for_review": True},
        {"grounded": True, "violations": []},
    ])
    pipe = _fit_pipeline(llm)
    out = pipe.process(_fit_email("measurements for the tavi pant in sizes 6, 8 and 10?", mid="fit2"))

    # The approach text mentions the field by name; the retrieved JSON key is the tell.
    assert '"garment_measurements"' not in llm.calls[1]["user"]
    assert "no spec on file" in out.reason


# ------------------------------------------------------------------ per-country prices


def test_zero_prices_are_the_suppressed_duplicates_and_never_quoted():
    """The site's $0 jewelry pages are a workaround, not a price. Parse, discard zeros,
    quote from what's left — and prefer the non-suffixed original handle."""
    shop = FixtureShopify(path=FIXTURES / "shopify" / "orders.json")
    facts = shop.price_lookup("Lone Diamond Pendant Necklace", "IT")
    assert facts.found
    assert facts.fields["price_min"] == "4890"
    assert facts.fields["price_max"] == "5120"
    assert facts.fields["price_currency"] == "EUR"
    assert "0" not in (facts.fields["price_min"], facts.fields["price_max"])


def test_a_product_with_only_zero_prices_escalates_instead_of_quoting_zero():
    shop = FixtureShopify(path=FIXTURES / "shopify" / "orders.json")
    facts = shop.price_lookup("Emerald Solitaire Ring", "IT")
    assert not facts.found
    assert "human must confirm" in facts.error


def test_an_unknown_country_maps_to_no_code_never_a_guess():
    from aflalo_cs.shopify import country_code

    assert country_code("Italy") == "IT"
    assert country_code("the UK") is None      # prose fragments don't sneak through
    assert country_code("UK") == "GB"
    assert country_code("it") == "IT"          # already-ISO input accepted
    assert country_code("Narnia") is None
    assert country_code(None) is None


def _price_classification(country="Italy", product="Lone Diamond Pendant Necklace"):
    return {
        "signals": {**{k: False for k in Signals().as_dict()}, "asks_about_money_amount": True},
        "rationale": "price", "category": "jewelry_price_inquiry", "risk": "medium",
        "order_identifier": None, "customer_first_name": "Benedetta",
        "country": country, "product_mentioned": product,
    }


def test_a_price_question_with_country_drafts_a_gated_reply_with_the_range():
    """End to end: money signal exempted for this category, price retrieved for HER country,
    range lands in the prompt, decision is manager-gated — a human confirms every number."""
    llm = FakeLLM(responses=[
        _price_classification(),
        {"draft_text": "Hi Benedetta,\n\nFor delivery to Italy, the Lone Diamond Pendant "
                       "Necklace is between €4,890 and €5,120 depending on the length. "
                       "Duties, taxes, and shipping can vary by destination, and checkout "
                       "will show your final total.\n\nWarmly, Eva",
         "flag_for_review": False},
        {"grounded": True, "violations": []},
    ])
    pipe = _fit_pipeline(llm, store_path="/tmp/aflalo_price.db")
    out = pipe.process(Email(message_id="p1", thread_id="T-p1", sender="b@x.it",
                             subject="Lone Diamond Pendant Necklace",
                             body="Hello, what is the price? I am in Italy. — Benedetta"))
    assert out.decision is Decision.NEEDS_MANAGER_APPROVAL
    assert "4890" in llm.calls[1]["user"]              # the retrieved range reached the drafter
    assert "price_disclaimer" in llm.calls[1]["user"]
    assert "Lone Diamond Pendant Necklace for IT attached" in out.reason


def test_a_price_question_without_a_country_confirms_before_quoting_anything():
    """Team rule (LW): confirm the ship-to country BEFORE providing any quote. The price
    fields are stripped from the drafter's facts, so a number cannot slip out even
    accidentally; the draft is the one-question ask."""
    llm = FakeLLM(responses=[
        _price_classification(country=None),
        {"draft_text": "Hi Benedetta,\n\nThank you for reaching out! In order to provide "
                       "you accurate pricing, can you please confirm the country the Lone "
                       "Diamond Pendant Necklace would ship to?\n\nWarmly, Eva",
         "flag_for_review": False},
        {"grounded": True, "violations": []},
    ])
    pipe = _fit_pipeline(llm, store_path="/tmp/aflalo_price2.db")
    out = pipe.process(Email(message_id="p2", thread_id="T-p2", sender="b@x.it",
                             subject="Lone Diamond Pendant Necklace",
                             body="How much is the pendant?"))
    assert out.decision is Decision.NEEDS_MANAGER_APPROVAL
    assert "quotes nothing" in out.reason
    prompt = llm.calls[1]["user"]
    assert '"price_min"' not in prompt                 # nothing quotable in the facts
    assert '"price_currency"' not in prompt


def test_an_all_zero_lookup_routes_to_a_human_not_a_zero_quote():
    llm = FakeLLM(responses=[_price_classification(product="Emerald Solitaire Ring")])
    pipe = _fit_pipeline(llm, store_path="/tmp/aflalo_price3.db")
    out = pipe.process(Email(message_id="p3", thread_id="T-p3", sender="b@x.it",
                             subject="Emerald ring price",
                             body="Price for the Emerald Solitaire Ring? I'm in Italy."))
    assert out.decision is Decision.NEEDS_HUMAN
    assert "price lookup failed" in out.reason
    assert out.draft_text is None


def test_a_price_not_in_retrieved_data_is_an_ungrounded_amount_in_euros_too():
    """MONEY_RE now sees €/£. A draft inventing €3,000 when the retrieved range is
    4890-5120 must be withheld by layer 1 alone."""
    from aflalo_cs.verify import layer1

    facts = OrderFacts(found=True, fields={"price_min": "4890.00", "price_max": "5120.00"})
    bad = Draft(text="It's around €3,000.00 for the small one.", flag_for_review=False)
    good = Draft(text="For Italy it is between €4,890.00 and €5,120.00.", flag_for_review=False)
    assert any("ungrounded amount" in v for v in layer1(bad, facts))
    assert layer1(good, facts) == []


# ------------------------------------------------------------------ business mail + scoping


def test_business_categories_draft_skeletons_without_tripping_their_own_signals():
    """A vendor pitch always 'asks about money' and often names a deadline. For these
    categories that's noise, not risk — the declared exemptions let the skeleton draft
    through while angry/legal/dispute signals still escalate."""
    from aflalo_cs.models import Classification
    from aflalo_cs.router import route

    sig = Signals(asks_about_money_amount=True, time_sensitive_deadline=True)
    cls = Classification(signals=sig, rationale="", category="vendor_or_agency_pitch", risk="low")
    assert route(cls).decision is Decision.DRAFT

    # Anger is never exempted — a furious anyone goes to a person.
    angry = Classification(signals=Signals(angry_or_threatening=True), rationale="",
                           category="vendor_or_agency_pitch", risk="low")
    assert route(angry).decision is Decision.NEEDS_HUMAN


def test_business_templates_carry_no_facts_and_no_commitments():
    """The skeletons exist so a human edits instead of writing from scratch — they must
    never state numbers, name tools, or leave an opening for a follow-up sequence."""
    for cat in ("recruiting_or_talent", "vendor_or_agency_pitch",
                "partnership_or_wholesale", "billing_or_invoice"):
        c = knowledge.get_category(cat)
        assert c.get("customer_facing") is False, cat
        tpl = c.get("template") or ""
        assert tpl and "Warmly, Eva" in tpl, cat
        assert not re.search(r"\d", tpl), f"{cat} template contains a number"


def test_customer_facing_defaults_to_true_for_the_unknown():
    assert knowledge.is_customer_facing("fit_question") is True
    assert knowledge.is_customer_facing("vendor_or_agency_pitch") is False
    assert knowledge.is_customer_facing(None) is True       # unprocessed thread = customer
    assert knowledge.is_customer_facing("never_heard_of_it") is True


def test_kpi_summary_scopes_customers_separately_from_vendor_noise():
    """A 20-minute reply to a sales pitch must not flatter the customer SLA."""
    store = _kpi_store("/tmp/aflalo_kpi_scope.db")
    from aflalo_cs.models import Outcome

    # Classify K-B (answered in 2h) as a vendor pitch; K-A stays customer mail.
    store.record(Outcome(message_id="kb1", decision=Decision.DRAFT, reason="x",
                         triggered_by="category", category="vendor_or_agency_pitch",
                         risk="low", signals={}), thread_id="K-B")
    k = store.kpi_summary()
    assert k["threads"] == 4 and k["customers"]["threads"] == 3
    assert k["first_response_median_h"] == 2.0            # all mail: 1, 2, 3
    assert k["customers"]["first_response_median_h"] == 2.0  # customers: 1, 3
    assert k["customers"]["resolved"] == 1                # K-B's resolution no longer counted


def test_thread_kpis_carry_the_category_for_airtable():
    from aflalo_cs.airtable import _thread_row_from
    from aflalo_cs.models import Outcome

    store = _kpi_store("/tmp/aflalo_kpi_cat.db")
    store.record(Outcome(message_id="kb1", decision=Decision.DRAFT, reason="x",
                         triggered_by="category", category="vendor_or_agency_pitch",
                         risk="low", signals={}), thread_id="K-B")
    rows = {r["thread_id"]: _thread_row_from(r) for r in store.thread_kpis()}
    assert rows["K-B"]["Category"] == "vendor_or_agency_pitch"
    assert rows["K-B"]["Is CS"] is False
    assert rows["K-C"]["Category"] is None
    assert rows["K-C"]["Is CS"] is True


# ------------------------------------------------------------------ follow-up owed


def _commitment_store(tmp="/tmp/aflalo_followup.db"):
    Path(tmp).unlink(missing_ok=True)
    s = Store(tmp)
    s.save_inbox([
        # F-A: we answered with a promise to retrieve — and never came back. OWED.
        _Msg("fa1", "F-A", "inbound",  "2026-08-10T10:00:00Z", "What is the inseam on the Sagan jeans?"),
        _Msg("fa2", "F-A", "outbound", "2026-08-10T11:00:00Z",
             "Hi! Let me pull the exact inseam on the Sagan jeans and come back to you.\n\nWarmly, Eva"),
        # F-B: answered completely. NOT owed.
        _Msg("fb1", "F-B", "inbound",  "2026-08-11T10:00:00Z", "Do you ship to Italy?"),
        _Msg("fb2", "F-B", "outbound", "2026-08-11T11:00:00Z",
             "We do ship to Italy — duties are included at checkout.\n\nWarmly, Eva"),
        # F-C: a promise contingent on an outside event. Following up NOW would be wrong.
        _Msg("fc1", "F-C", "inbound",  "2026-08-12T10:00:00Z", "Will the Lido pant restock?"),
        _Msg("fc2", "F-C", "outbound", "2026-08-12T11:00:00Z",
             "Yes! I'll let you know the moment it's back in stock.\n\nWarmly, Eva"),
        # F-D: we promised AND she wrote again after — the ordinary unanswered case wins.
        _Msg("fd1", "F-D", "inbound",  "2026-08-13T10:00:00Z", "Measurements for the Dara?"),
        _Msg("fd2", "F-D", "outbound", "2026-08-13T11:00:00Z", "Let me check and get back to you!\n\nWarmly, Eva"),
        _Msg("fd3", "F-D", "inbound",  "2026-08-14T09:00:00Z", "Any update?"),
    ])
    return s


def test_a_thread_where_our_reply_promised_retrieval_is_still_unresolved():
    """Timestamps say F-A is answered. The content says the customer is waiting for the
    inseam we promised — that thread must re-enter the queue, flagged."""
    emails = {e.message_id: e for e in _commitment_store().inbox()}
    assert "fa1" in emails and emails["fa1"].followup_owed is True
    assert "fb1" not in emails                       # fully answered
    assert "fd3" in emails and emails["fd3"].followup_owed is False  # she re-wrote; case 1


def test_a_promise_contingent_on_an_outside_event_is_not_owed_yet():
    """'I'll let you know when it's back in stock' is a commitment we can't fulfil until
    the restock happens. Drafting a follow-up now would be a wrong email."""
    assert "fc1" not in {e.message_id for e in _commitment_store().inbox()}


def test_followup_owed_is_annotated_in_the_routing_reason():
    llm = FakeLLM(responses=[
        _fit_classification(),
        {"draft_text": "Hi Ana,\n\nHere are those measurements you asked about — the Aire "
                       "Dress in a 4 measures 28.5 inches at the chest.\n\nWarmly, Eva",
         "flag_for_review": False},
        {"grounded": True, "violations": []},
    ])
    store = _commitment_store("/tmp/aflalo_followup2.db")
    from aflalo_cs.gmail_client import StoredMailbox

    pipe = Pipeline(mailbox=StoredMailbox(store=store), llm=llm,
                    shop=FixtureShopify(path=FIXTURES / "shopify" / "orders.json"), store=store)
    email = next(e for e in store.inbox() if e.followup_owed)
    out = pipe.process(email)
    assert "follow-up owed" in out.reason


def test_reprocess_reruns_what_was_already_handled_and_keeps_the_audit_trail():
    llm = FakeLLM(responses=[
        _fit_classification(),
        {"draft_text": GOOD_DRAFT, "flag_for_review": False},
        {"grounded": True, "violations": []},
    ] * 2)
    store = _commitment_store("/tmp/aflalo_reproc.db")
    from aflalo_cs.gmail_client import StoredMailbox

    pipe = Pipeline(mailbox=StoredMailbox(store=store), llm=llm,
                    shop=FixtureShopify(path=FIXTURES / "shopify" / "orders.json"), store=store)
    first = pipe.run(limit=1)
    assert len(first) == 1
    assert pipe.run(limit=1) == []                    # dedup holds by default
    again = pipe.run(limit=1, reprocess=True)         # ...until asked to re-draft
    assert len(again) == 1
    with store._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM drafts WHERE message_id = ?",
                      (first[0].message_id,)).fetchone()[0]
    assert n == 2                                     # both drafts kept for the audit trail


# ------------------------------------------------------------------ impersonation / scam net


def _scam_email(body, sender, subject="Aflalo NYC - Katy Perry request", mid="scam1", thread="T-scam"):
    return Email(message_id=mid, thread_id=thread, sender=sender, subject=subject, body=body)


KATY_PERRY_BODY = (
    "Dear Team, Wondering if we could loan the below two looks for Katy Perry for the "
    "premiere of her Lifetimes Tour film next week in 26th of August? Looking forward to "
    "your thoughts. Kindest, Heather"
)


def test_the_real_katy_perry_specimen_is_flagged():
    """The actual email from the inbox: loan request + celebrity premiere + deadline.
    Three weak marks — flagged, with each mark named so the human knows what to verify."""
    from aflalo_cs import phishing

    marks = phishing.assess(_scam_email(KATY_PERRY_BODY, "Heather P <heather@heatherpicchiottino.com>"))
    assert len(marks) >= 2
    joined = " | ".join(marks)
    assert "loan/pull/gifting" in joined and "celebrity event" in joined
    assert "verified through a known channel" in phishing.advice(marks)


def test_the_freemail_followup_in_a_professional_thread_is_a_mark():
    """The specimen's giveaway: the chase arrives from julianavargasr30@gmail.com in a
    thread that began at a stylist domain."""
    from aflalo_cs import phishing

    marks = phishing.assess(
        _scam_email("Hi!! Kindly following up here. Please let us know if you can help xx "
                    "re: the looks to pull for the premiere",
                    "Juliana Vargas <julianavargasr30@gmail.com>",
                    subject="Re: Aflalo NYC - Katy Perry request"),
        prior_senders=["Heather P <heather@heatherpicchiottino.com>"],
    )
    assert any("personal address" in m and "heatherpicchiottino.com" in m for m in marks)


def test_ordinary_cs_mail_is_not_flagged():
    """A customer asking about her order from gmail is the NORMAL case. One weak mark
    (or none) must never flag — the cost of crying wolf is the team ignoring the label."""
    from aflalo_cs import phishing

    assert phishing.assess(_scam_email(
        "Hi! Where is my order #7412? I need it by Friday!", "Ana <ana.b@gmail.com>",
        subject="Order 7412")) == []
    assert phishing.assess(_scam_email(
        "Could you send over the measurements for the Dara dress?", "Mia <mia@gmail.com>",
        subject="Sizing")) == []


def test_strong_marks_flag_alone():
    from aflalo_cs import phishing

    lookalike = phishing.assess(_scam_email("Hello, please update our records.",
                                            "Accounts <billing@aflalo-nyc.co>", subject="Records"))
    assert any("imitates ours" in m for m in lookalike)

    banking = phishing.assess(_scam_email(
        "Please note our updated bank details for the outstanding invoice.",
        "Vendor <accounts@supplier-co.com>", subject="Invoice"))
    assert any("vendor-fraud" in m for m in banking)


def test_a_flagged_email_gets_no_draft_no_model_call_and_the_quarantine_label():
    """The net fires before the classifier: zero model calls, no draft of any kind (even a
    decline confirms a live inbox), the phishing label instead of cs/no-draft, and the
    business-mail skeletons never see it."""
    from aflalo_cs.models import PHISHING_LABEL

    llm = FakeLLM(responses=[])
    store = _commitment_store("/tmp/aflalo_phish.db")
    pipe = Pipeline(mailbox=MockMailbox(path=FIXTURES / "mock_inbox.json"), llm=llm,
                    shop=FixtureShopify(path=FIXTURES / "shopify" / "orders.json"), store=store)
    out = pipe.process(_scam_email(KATY_PERRY_BODY, "Heather P <heather@heatherpicchiottino.com>"))
    assert out.decision is Decision.NEEDS_HUMAN
    assert out.category == "suspected_phishing_or_scam"
    assert out.draft_text is None
    assert out.label == PHISHING_LABEL
    assert llm.calls == []                      # the model never even saw it


def test_the_phishing_signal_cannot_be_exempted_by_a_business_category():
    """A con dressed as a partnership pitch must not get the partnership skeleton's warm
    acknowledgment — signal_exemptions cannot loosen this one."""
    from aflalo_cs.models import Classification
    from aflalo_cs.router import route

    cls = Classification(signals=Signals(possible_phishing_or_scam=True), rationale="",
                         category="partnership_or_wholesale", risk="low")
    got = route(cls)
    assert got.decision is Decision.NEEDS_HUMAN
    assert "phishing" in got.reason


# ------------------------------------------------------------------ answer-first retrieval


def test_where_do_you_ship_is_answered_from_shop_facts_not_deflected():
    """'Do you take orders from other countries or just the UK?' — a real email. The
    ships-to list is retrieved once and lands in the policy block, so the drafter can
    answer 'we ship worldwide' instead of asking her where she lives."""
    llm = FakeLLM(responses=[
        {**_fit_classification(), "category": "general_policy_question"},
        {"draft_text": "Hi there,\n\nYes, we ship worldwide, the UK included. Duties and "
                       "taxes are determined at checkout, so the price you see is complete."
                       "\n\nWarmly, Eva",
         "flag_for_review": False},
        {"grounded": True, "violations": []},
    ])
    pipe = _fit_pipeline(llm, store_path="/tmp/aflalo_ship.db")
    out = pipe.process(Email(message_id="s1", thread_id="T-s1", sender="d@gmail.com",
                             subject="Reg shipping:",
                             body="Aflalo Do you take orders from other countries or just the U.k?"))
    assert out.decision is Decision.DRAFT
    # The composed policy reached BOTH the drafter and the verifier.
    assert "worldwide" in str(llm.calls[1]["system"])
    assert "worldwide" in str(llm.calls[2]["user"])


def test_a_named_style_with_no_spec_still_gets_catalog_answers():
    """Carolyn's Tavi Pant: the Drive folder has no measurements, but Shopify knows sizes,
    stock, price, and the description. The drafter must receive all of it."""
    llm = FakeLLM(responses=[
        {**_fit_classification(), "product_mentioned": "Tavi Pant"},
        {"draft_text": "Hi Ana,\n\nThe Tavi Pant runs sizes 0 to 10 and is a low-rise pant "
                       "with a wide leg in Italian wool twill. Size 4 is sold out right "
                       "now; the rest are in stock. I can send the full garment "
                       "measurements for any size.\n\nWarmly, Eva",
         "flag_for_review": False},
        {"grounded": True, "violations": []},
    ])
    pipe = _fit_pipeline(llm, store_path="/tmp/aflalo_catalog.db")
    out = pipe.process(_fit_email("What sizes does the Tavi Pant come in?", mid="cat1"))
    prompt = llm.calls[1]["user"]
    assert '"sizes_in_stock"' in prompt and '"sizes_sold_out"' in prompt
    import json as _json
    blob = prompt.rsplit("RETRIEVED DATA", 1)[1]   # the approach text mentions it too
    facts = _json.loads(blob[blob.index("{"): blob.rindex("}") + 1])
    assert facts["sizes_sold_out"] == ["4"]
    assert facts["sizes_in_stock"] == ["0", "2", "6", "8", "10"]
    assert "wool twill" in prompt                       # description is quotable material
    assert '"price_min": "670"' in prompt
    assert out.decision is Decision.DRAFT


def test_catalog_facts_report_availability_even_when_every_price_is_suppressed():
    """'What sizes does the pendant come in' deserves an answer even on a $0-suppressed
    product — availability isn't a price."""
    shop = FixtureShopify(path=FIXTURES / "shopify" / "orders.json")
    facts = shop.product_lookup("Emerald Solitaire Ring")
    assert facts.found
    assert "price_min" not in facts.fields              # zeros never become a price
    assert facts.fields["sizes_in_stock"] == ["6"]


def test_the_description_grounds_numbers_the_drafter_quotes_from_it():
    """The Sagan's 32-inch inseam lives in its product description. Once retrieved, quoting
    it must pass layer 1 — the description string is part of the haystack."""
    shop = FixtureShopify(path=FIXTURES / "shopify" / "orders.json")
    facts = shop.product_lookup("Sagan jeans")
    assert "32 inch inseam" in facts.fields["product_description"]
    hay = " | ".join(facts.groundable_values())
    assert "32 inch inseam" in hay


def test_policy_digest_allows_retrieved_prices_and_forbids_remembered_ones():
    """The digest is what verify layer 2 reads as law. Before this, it said 'never state
    any price' — written pre-retrieval — so the verifier killed every legitimately quoted,
    Shopify-sourced price."""
    d = knowledge.policy_digest()
    assert "MAY be stated when it appears in RETRIEVED DATA" in d
    assert "any price not present in retrieved data" in d


def test_the_verifier_sees_who_the_email_is_from():
    """Greeting Daniel by the display name on his own email is not an invented fact — but
    the verifier can only know that if the From header is in its customer-email source."""
    llm = FakeLLM(responses=[
        {**_fit_classification(), "category": "general_policy_question",
         "customer_first_name": "Daniel"},
        {"draft_text": "Hello Daniel,\n\nYes — we ship worldwide.\n\nWarmly, Eva",
         "flag_for_review": False},
        {"grounded": True, "violations": []},
    ])
    pipe = _fit_pipeline(llm, store_path="/tmp/aflalo_from.db")
    pipe.process(Email(message_id="f1", thread_id="T-f1",
                       sender="Daniel Tech <glamourgadget.neckband@gmail.com>",
                       subject="Reg shipping:", body="Do you take orders from other countries?"))
    assert "From: Daniel Tech" in llm.calls[2]["user"]   # layer 2's payload


# ------------------------------------------------------------------ fill-in + provenance


def test_a_missing_fact_becomes_a_blank_not_a_promise_to_check():
    """'I'll pull it and come back' hands the reviewer an errand. A blank hands them a
    finished draft minus one number — fill in, send."""
    llm = FakeLLM(responses=[
        _fit_classification(),
        {"draft_text": "Hi Elizabeth,\n\nThe inseam on the Sagan in a 27 is ______. If "
                       "there's a pair you already love, tell me and I'll help you compare."
                       "\n\nWarmly, Eva",
         "flag_for_review": True},
        {"grounded": True, "violations": []},
    ])
    pipe = _fit_pipeline(llm, store_path="/tmp/aflalo_blank.db")
    out = pipe.process(_fit_email("What is the inseam on the Sagan jeans?", mid="bl1"))
    assert out.decision is Decision.DRAFT_UNVERIFIED
    assert "FILL-IN" in out.reason and "fill in and send" in out.reason


def test_underscore_blanks_are_not_placeholder_violations():
    """PLACEHOLDER_RE hunts {curly} and [bracket] leftovers. Deliberate ______ blanks are
    the sanctioned fill-in mechanism and must pass layer 1."""
    from aflalo_cs.verify import layer1

    ok = layer1(Draft(text="The inseam on the Sagan in a 27 is ______."), OrderFacts(found=True))
    assert ok == []


def test_provenance_says_yes_and_names_the_source_with_currency_and_country():
    from aflalo_cs.airtable import _provenance

    got = _provenance({
        "reason": "category ok | price: X for IT attached",
        "draft_text": "For Italy it is between 4890.00 and 5120.00 EUR.",
        "fields_fetched": json.dumps({"price_min": "4890.00", "price_currency": "EUR",
                                      "price_country": "IT", "product": "Pendant"}),
        "category": "jewelry_price_inquiry",
    })
    assert got["Info Available"] == "Yes"
    assert "EUR" in got["Info Source"] and "IT" in got["Info Source"]


def test_provenance_says_no_when_the_asked_fact_was_missing():
    from aflalo_cs.airtable import _provenance

    got = _provenance({
        "reason": "PARTIAL | sizing: no spec on file for the style asked about",
        "draft_text": "The inseam on the Sagan in a 27 is ______.",
        "fields_fetched": json.dumps({"sizes_in_stock": ["25", "26"]}),
        "category": "fit_question",
    })
    assert got["Info Available"] == "No"
    assert "MISSING" in got["Info Source"]
    assert "spec folder" in got["Info Source"]


def test_provenance_is_na_for_business_mail():
    from aflalo_cs.airtable import _provenance

    got = _provenance({"reason": "x", "draft_text": "Hi", "fields_fetched": None,
                       "category": "vendor_or_agency_pitch"})
    assert got["Info Available"] == "n/a"


def test_restock_questions_are_answerable_from_incoming_inventory():
    """'Is it coming back in stock?' — read_inventory exposes incoming units, so the draft
    can say a replenishment is on its way instead of blanking everything. The DATE still
    blanks (needs read_inventory_shipments)."""
    shop = FixtureShopify(path=FIXTURES / "shopify" / "orders.json")
    facts = shop.product_lookup("Tavi Pant")
    assert facts.fields["sizes_sold_out"] == ["4"]
    assert facts.fields["sizes_restock_incoming"] == ["4"]


def test_stacked_invitations_fail_the_voice_lint():
    """The deck: one line of warmth is enough. Two open invitations in one email is the
    hand-holding it bans — caught deterministically, found in a real live draft."""
    from aflalo_cs.voice_lint import lint

    chatty = ("Hi Elizabeth,\n\nThe inseam is 32 inches.\n\nIf there's a pair you already "
              "own and love, tell me and I'll help you compare. And if you have a size in "
              "mind, let me know and I'll send those numbers.\n\nWarmly, Eva")
    assert any("one line of warmth" in v for v in lint(chatty))

    restrained = ("Hi Elizabeth,\n\nThe inseam is 32 inches, the garment's "
                  "finished measurement.\n\nWarmly, Eva")
    assert lint(restrained) == []


def test_trailing_filler_fails_the_lint_on_sight():
    """Her exact example: the tracking answer was complete, then grew a comfort sentence.
    The draft ends when the answer ends."""
    from aflalo_cs.voice_lint import lint

    fluffy = ("Hi Emily,\n\nGood news, order #7278 is on its way. Tracking 2163669185: "
              "https://dhl.example/track\n\nIf anything looks off with the tracking, just "
              "tell me and I'll take it from there.\n\nWarmly, Eva")
    got = lint(fluffy)
    assert any("draft ends when the answer ends" in v for v in got)

    tight = ("Hi Emily,\n\nGood news, order #7278 is on its way. Tracking 2163669185: "
             "https://dhl.example/track\n\nWarmly, Eva")
    assert lint(tight) == []


# ------------------------------------------------------------------ semantic thread closure


def _closure_store(tmp="/tmp/aflalo_closure.db"):
    Path(tmp).unlink(missing_ok=True)
    s = Store(tmp)
    s.save_inbox([
        # C-A: answered, she confirms with thanks -> Resolved — confirmed, stamped at OUR answer.
        _Msg("ca1", "C-A", "inbound",  "2026-08-20T10:00:00Z", "Do you ship to Italy?"),
        _Msg("ca2", "C-A", "outbound", "2026-08-20T12:00:00Z", "We do — worldwide.\n\nWarmly, Eva"),
        _Msg("ca3", "C-A", "inbound",  "2026-08-20T13:00:00Z", "Perfect, thank you so much!"),
        # C-B: 'thanks BUT' — gratitude that reopens. Must stay Open and stay in the queue.
        _Msg("cb1", "C-B", "inbound",  "2026-08-21T10:00:00Z", "Where is order 7412?"),
        _Msg("cb2", "C-B", "outbound", "2026-08-21T11:00:00Z", "It's on its way.\n\nWarmly, Eva"),
        _Msg("cb3", "C-B", "inbound",  "2026-08-21T15:00:00Z",
             "Thanks, but it still hasn't arrived and I'm getting worried."),
    ])
    return s


def test_a_closing_thanks_resolves_the_thread_confirmed_and_stamps_the_answer():
    """Her 'perfect, thank you!' is the customer confirming resolution — case (b). The
    resolution timestamp is OUR answer (12:00), not her thanks: 2h, not 3h."""
    k = {x["thread_id"]: x for x in _closure_store().thread_kpis()}
    a = k["C-A"]
    assert a["thread_status"] == "Resolved — confirmed"
    assert a["resolution_hours"] == 2.0
    assert a["resolved_at"] == "2026-08-20T12:00:00Z"
    assert a["open_hours"] is None                       # the ageing clock stopped


def test_thanks_but_it_still_has_not_arrived_never_closes_a_thread():
    k = {x["thread_id"]: x for x in _closure_store().thread_kpis()}
    assert k["C-B"]["thread_status"] == "Open"
    assert k["C-B"]["resolution_hours"] is None


def test_a_closing_thanks_is_not_queued_for_a_draft_but_a_reopener_is():
    """Drafting a reply to 'thank you!' is noise. Drafting one to 'thanks but it hasn't
    arrived' is the job."""
    ids = {e.message_id for e in _closure_store().inbox()}
    assert "ca3" not in ids
    assert "cb3" in ids


def test_kpi_summary_reports_averages_in_hours():
    k = _closure_store().kpi_summary()
    assert k["resolved_confirmed"] == 1
    assert k["first_response_avg_h"] == 1.5              # (2h + 1h) / 2
    assert k["resolution_avg_h"] == 2.0


def test_published_policy_amounts_ground_in_layer1():
    """A draft citing the published $20 return fee must not be withheld as an invented
    amount: policy joins the layer-1 haystack. An amount in neither source still fails."""
    from aflalo_cs.verify import layer1

    policy = knowledge.policy_digest()
    ok = Draft(text="A refund carries a $20 fee, waived if you choose store credit.")
    bad = Draft(text="A refund carries a $35 fee.")
    assert layer1(ok, OrderFacts(found=True), policy) == []
    assert any("ungrounded amount" in v for v in layer1(bad, OrderFacts(found=True), policy))


def test_em_dashes_fail_the_voice_lint():
    from aflalo_cs.voice_lint import lint

    assert any("dash" in v for v in lint(
        "Hi Ana,\n\nGood news — your order is on its way.\n\nWarmly, Eva"))
    assert lint("Hi Ana,\n\nGood news: your order is on its way.\n\nWarmly, Eva") == []


def test_status_categories_now_retrieve_tracking():
    for cat in ("order_status_shipping_timeline", "preorder_ship_timing"):
        fields = knowledge.required_fields(cat)
        assert "tracking_number" in fields and "tracking_url" in fields, cat


# ------------------------------------------------------------------ refund asks draft, gated


def test_a_polite_refund_ask_gets_a_gated_draft_not_nothing():
    """Brenda's real email: damaged pants, photo attached, 'could you please advise on a
    refund?' The old rule stripped the draft entirely; now the remedy stays a manager
    decision but the human starts from a draft."""
    from aflalo_cs.models import Classification
    from aflalo_cs.router import route

    cls = Classification(
        signals=Signals(demands_refund_or_cancel=True, photos_already_attached=True),
        rationale="", category="damaged_item", risk="medium",
    )
    got = route(cls)
    assert got.decision is Decision.NEEDS_MANAGER_APPROVAL
    assert "refund/cancellation" in got.reason and "manager decision" in got.reason


def test_an_angry_refund_demand_still_gets_no_draft():
    """The split that makes this safe: heat is its own signal, and it stays hard."""
    from aflalo_cs.models import Classification
    from aflalo_cs.router import route

    cls = Classification(
        signals=Signals(demands_refund_or_cancel=True, angry_or_threatening=True),
        rationale="", category="damaged_item", risk="medium",
    )
    got = route(cls)
    assert got.decision is Decision.NEEDS_HUMAN
    assert "angry" in got.reason


def test_a_refund_ask_on_a_human_only_category_stays_human():
    from aflalo_cs.models import Classification
    from aflalo_cs.router import route

    # `other` is the last human-only customer category — no documented scenario at all.
    cls = Classification(signals=Signals(demands_refund_or_cancel=True),
                         rationale="", category="other", risk="low")
    assert route(cls).decision is Decision.NEEDS_HUMAN


def test_the_formerly_human_categories_now_draft_from_published_policy():
    """Exchanges, discount codes, and alterations all have documented answers now (the
    site's refund policy, the no-codes fact, the alteration policy) — so they draft.
    Messenger stays manager-gated: eligibility lives on a human-held VIP list."""
    assert knowledge.tier("exchange_request") == "draft"
    assert knowledge.tier("discount_code_request") == "draft"
    assert knowledge.tier("alteration_or_resize_request") == "draft"
    assert knowledge.tier("messenger_delivery_request") == "manager"
    assert knowledge.tier("upcoming_products_question") == "draft"


def test_deadline_and_dispute_gate_the_draft_instead_of_stripping_it():
    from aflalo_cs.models import Classification
    from aflalo_cs.router import route

    for sig, needle in (("time_sensitive_deadline", "deadline"),
                        ("delivery_dispute", "carrier record")):
        cls = Classification(signals=Signals(**{sig: True}), rationale="",
                             category="where_is_my_order", risk="low",
                             order_identifier="7412")
        got = route(cls)
        assert got.decision is Decision.NEEDS_MANAGER_APPROVAL, sig
        assert needle in got.reason


def test_our_own_replies_are_outbound_even_when_found_in_inbox():
    """Copies of team replies land in INBOX (client save behavior, self-CCs) and were
    tagged inbound by the folder rule — polluting the queue and the response clocks. The
    From header wins, and already-stored rows are migrated on open."""
    tmp = "/tmp/aflalo_direction.db"
    Path(tmp).unlink(missing_ok=True)
    s = Store(tmp)
    s.save_inbox([
        _Msg("d1", "D-A", "inbound", "2026-08-20T10:00:00Z", "Where is my order?"),
        _Msg("d2", "D-A", "inbound", "2026-08-20T11:00:00Z",
             "It's on its way!\n\nWarmly, Eva", sender="AFLALO <aflalo@aflalonyc.com>"),
    ])
    s2 = Store(tmp)  # reopen -> migration runs
    with s2._conn() as c:
        d = c.execute("SELECT direction FROM inbox_messages WHERE message_id='d2'").fetchone()[0]
    assert d == "outbound"
    assert "d2" not in {e.message_id for e in s2.inbox()}   # never queued
    k = {x["thread_id"]: x for x in s2.thread_kpis()}
    assert k["D-A"]["first_response_hours"] == 1.0          # counts as OUR reply now


def test_resolved_threads_clear_out_of_the_airtable_queue():
    from aflalo_cs.airtable import _row_from

    fossil = {"message_id": "x", "signals": "{}", "label_applied": "cs/no-draft",
              "thread_status": "Resolved — confirmed", "draft_text": None}
    assert _row_from(fossil)["Status"] == "Thread resolved"
    live = {"message_id": "y", "signals": "{}", "label_applied": "cs/ready-to-send",
            "thread_status": "Awaiting first reply", "draft_text": "Hi"}
    assert _row_from(live)["Status"] == "Ready to send"


def test_a_heartfelt_thanks_closes_the_thread_even_when_long_and_narrative():
    """Jen's real message: 330 characters, contains 'when' narratively, zero asks. It was
    drafted against because the ack detector was too strict. Never again."""
    from aflalo_cs.store import is_closing_ack

    jen = ("Eva, Thank you so very much for your understanding. I will not soon forget it. "
           "And someday, when we are in a different place, I will return to shop with you "
           "again and again. Please know that this exception has made a giant difference "
           "to me and I am deeply grateful to you. Jen")
    assert is_closing_ack(jen) is True

    # The guards that must survive the loosening:
    assert is_closing_ack("Thanks, but when will it actually arrive?") is False   # question
    assert is_closing_ack("Thank you, but it still hasn't arrived.") is False     # complaint
    assert is_closing_ack("Thanks! Please send the label to my new address.") is False  # request


def test_a_closing_thanks_resolves_even_when_our_reply_is_missing_from_the_store():
    """Jen's case: the answer she's thanking us for was sent from a mailbox we don't
    import, so no outbound exists here. Her gratitude proves an answer happened — the
    thread closes as customer-confirmed, with timing blank rather than invented."""
    tmp = "/tmp/aflalo_oob.db"
    Path(tmp).unlink(missing_ok=True)
    s = Store(tmp)
    s.save_inbox([
        _Msg("j1", "J-A", "inbound", "2026-08-25T10:00:00Z", "Can you make an exception on this return?"),
        _Msg("j2", "J-A", "inbound", "2026-08-26T14:00:00Z",
             "Thank you so very much for your understanding. I will not soon forget it. "
             "This exception has made a giant difference and I am deeply grateful. Jen"),
    ])
    k = {x["thread_id"]: x for x in s.thread_kpis()}["J-A"]
    assert k["thread_status"] == "Resolved — confirmed"
    assert k["resolution_hours"] is None                 # timing unknown, never invented
    assert "j2" not in {e.message_id for e in s.inbox()}


# ------------------------------------------------------------------ LW review round


def test_no_problem_thank_you_closes_the_thread():
    """Tiana's real sign-off: "That's no problem. Thank you, I'll check the link!" —
    'problem' negated by 'no' is a closer, not a complaint. LW: needs no response."""
    from aflalo_cs.store import is_closing_ack

    assert is_closing_ack("That's no problem. Thank you, I'll check the link! Tiana") is True
    assert is_closing_ack("Thanks, but there's a problem with the zipper.") is False


def test_wholesale_drafts_carry_lani_as_cc():
    """LW's flow: wholesale loops in Lani, cc'd on the draft so the human sends with her
    already on the thread."""
    llm = FakeLLM(responses=[
        {**_fit_classification(), "category": "partnership_or_wholesale"},
        {"draft_text": "Hi Ido,\n\nThanks for reaching out, I am adding Lani, our head of "
                       "wholesale, who will get back to you!\n\nWarmly, Eva",
         "flag_for_review": False},
        {"grounded": True, "violations": []},
    ])
    mailbox = MockMailbox(path=FIXTURES / "mock_inbox.json")
    store = Store("/tmp/aflalo_cc.db")
    Path("/tmp/aflalo_cc.db").unlink(missing_ok=True)
    pipe = Pipeline(mailbox=mailbox, llm=llm,
                    shop=FixtureShopify(path=FIXTURES / "shopify" / "orders.json"),
                    store=Store("/tmp/aflalo_cc.db"))
    email = Email(message_id="w1", thread_id="T-w1", sender="ido@daniella-tlv.com",
                  subject="Showroom appointment", body="Can we book a showroom visit?")
    mailbox._emails = [email]
    pipe.run(limit=1)
    assert mailbox.ccs.get("w1") == "lani@aflalonyc.com"


def test_the_html_alternative_links_the_tracking_number_itself():
    """LW: 'would be ideal to link the URL on the tracking code itself.' The HTML part
    makes the number the anchor; the bare URL disappears from the rendered draft."""
    from aflalo_cs.gmail_client import _html_body

    html = _html_body("Good timing, your order is on its way.\n"
                      "Tracking 2163669185: https://dhl.example/track?id=2163669185\n\nWarmly, Eva")
    assert '<a href="https://dhl.example/track?id=2163669185">2163669185</a>' in html
    assert html.count("dhl.example") == 1              # URL lives only inside the anchor


def test_out_of_window_grace_rule_is_in_the_policy_digest():
    d = knowledge.policy_digest()
    assert "the team makes the label by hand" in d
    assert "beyond that we do not allow" in d
    assert "never the internal cutoff" in d


# ------------------------------------------------------------------ the learning loop


def test_lessons_flow_from_the_review_column_into_every_draft(tmp_path, monkeypatch):
    """The generalization mechanism: reviewer comments become lessons.md, and lessons.md
    is part of every draft's system prompt. Feedback changes behavior without code."""
    from aflalo_cs import feedback, knowledge as K
    from aflalo_cs.draft import build_draft
    from aflalo_cs.models import Classification, Email, OrderFacts, Signals

    monkeypatch.setattr(K, "KNOWLEDGE_DIR", tmp_path)
    path = feedback.write_lessons([
        "Answer only what was asked; add nothing adjacent (LW, 2026-09-01)",
    ])
    assert path.exists()
    assert "binding on every draft" in K.lessons()

    llm = FakeLLM(responses=[{"draft_text": GOOD_DRAFT, "flag_for_review": False}])
    build_draft(
        llm,
        Email(message_id="m", thread_id="t", sender="a@b.com", subject="Hi", body="Where is order #7412?"),
        Classification(signals=Signals(), rationale="", category="where_is_my_order", risk="low"),
        OrderFacts(found=True, fields={"order_number": "#7412"}),
        policy="policy",
    )
    system_text = str(llm.calls[0]["system"])
    assert "add nothing adjacent (LW, 2026-09-01)" in system_text


def test_the_universal_laws_generalize_the_review_round():
    """LW's eleven comments reduce to two laws that bind every category, including ones
    that don't exist yet — the fix for the class, not the instance."""
    from aflalo_cs.draft import SYSTEM_RULES

    assert "SCOPE." in SYSTEM_RULES and "nothing adjacent" in SYSTEM_RULES
    assert "MISSING VARIABLE." in SYSTEM_RULES
    assert "nothing provisional" in SYSTEM_RULES


def _block(**over):
    base = {"window_start": "2026-08-12", "window_end": "2026-08-26",
            "threads": 97, "answered": 74, "pct_answered": 76,
            "resolved": 72, "pct_resolved": 74, "resolved_confirmed": 14,
            "awaiting_first_reply": 23,
            "first_response_avg_h": 30.1, "first_response_median_h": 12.0,
            "first_response_p90_h": 93.1, "resolution_avg_h": 49.7,
            "resolution_median_h": 14.8, "resolution_p90_h": 165.0,
            "oldest_unanswered_h": 48.0,
            "received_business_hours": 60, "received_off_hours": 37,
            "frt_business_hours": {"count": 50, "avg_h": 20.0, "median_h": 8.0, "p90_h": 70.0},
            "frt_off_hours": {"count": 24, "avg_h": 45.0, "median_h": 18.0, "p90_h": 110.0}}
    base.update(over)
    return base


def test_kpi_rows_are_fiscal_weeks_then_totals_then_backlog():
    """Each row names its own period (Sarena, 2026-09-03); weeks use the team's 4-5-4
    calendar labels (2026-09-11); first response is split by business-hours arrival."""
    from aflalo_cs.airtable import _kpi_rows

    week = _block(threads=20, resolved=16, key="FY2026 W34 · AUG-D (week of 8/16)",
                  label="AUG-D", week_start="2026-08-16", week_end="2026-08-22", maturing=True)
    summary = {"customers": _block(), "weeks": [week]}
    w, total, now = _kpi_rows(summary, "2026-09-11T12:00:00+00:00")
    assert w["Period"] == "FY2026 W34 · AUG-D (week of 8/16)" and w["Fiscal Week"] == "AUG-D"
    assert w["Covers"] == "conversations started AUG-D, 2026-08-16 to 2026-08-22"
    assert w["Still maturing"] is True
    assert total["Period"] == "All time" and "2026-08-12 to 2026-08-26" in total["Covers"]
    assert total["Median First Response, Business Hours (hrs)"] == 8.0
    assert total["Median First Response, Off Hours (hrs)"] == 18.0
    assert total["Received in Business Hours"] + total["Received Off Hours"] == 97
    assert now["Period"] == "Right now" and now["Awaiting first reply"] == 23


def test_the_454_calendar_reproduces_the_teams_own_examples():
    """Given 2026-09-11: year starts 12/28/25; AUG-D = week of 8/16, AUG-E = 8/23,
    SEP-A = 8/30 (their 'AUG-C = 8/19' is a typo for 8/9 — 8/19 is a Wednesday)."""
    from datetime import date
    from aflalo_cs.retail_calendar import fiscal_week

    assert fiscal_week(date(2025, 12, 28)).label == "JAN-A"
    assert fiscal_week(date(2026, 1, 24)).label == "JAN-D"       # last day of a 4-week Jan
    assert fiscal_week(date(2026, 1, 25)).label == "FEB-A"       # Feb has 5 weeks
    assert fiscal_week(date(2026, 2, 28)).label == "FEB-E"
    assert fiscal_week(date(2026, 8, 9)).label == "AUG-C"
    fw = fiscal_week(date(2026, 8, 19))                          # a Wednesday, inside AUG-D
    assert (fw.label, fw.week_start) == ("AUG-D", date(2026, 8, 16))
    assert fiscal_week(date(2026, 8, 23)).label == "AUG-E"
    assert fiscal_week(date(2026, 8, 30)).label == "SEP-A"
    assert fiscal_week(date(2026, 12, 26)).label == "DEC-D"      # week 52
    nxt = fiscal_week(date(2026, 12, 27))                        # rolls into FY2027
    assert (nxt.label, nxt.fiscal_year, nxt.week_number) == ("JAN-A", 2027, 1)
    assert fw.key == "FY2026 W34 · AUG-D (week of 8/16)"


def test_business_hours_are_eastern_weekdays_nine_to_seven():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from aflalo_cs.retail_calendar import in_business_hours

    et = ZoneInfo("America/New_York")
    assert in_business_hours(datetime(2026, 8, 18, 9, 0, tzinfo=et))          # Tue 9:00 opens
    assert in_business_hours(datetime(2026, 8, 18, 18, 59, tzinfo=et))
    assert not in_business_hours(datetime(2026, 8, 18, 19, 0, tzinfo=et))     # 7pm closes
    assert not in_business_hours(datetime(2026, 8, 22, 12, 0, tzinfo=et))     # Saturday
    # Store times are UTC: 22:00Z in August is 6pm ET (open); 23:30Z is 7:30pm (closed)
    assert in_business_hours(datetime(2026, 8, 18, 22, 0, tzinfo=timezone.utc))
    assert not in_business_hours(datetime(2026, 8, 18, 23, 30, tzinfo=timezone.utc))
    assert not in_business_hours(datetime(2026, 8, 18, 23, 30))               # naive == UTC


def test_a_vip_signature_gets_a_manager_gated_draft_not_silence():
    """2026-09-11: a makeup artist with an agency signature asked to get her order sooner
    and got no draft at all, reason 'legal/press/VIP'. Press REQUESTS are caught earlier by
    the safety net; this signal on an ordinary customer question means 'careful', not 'no'."""
    r = router.route(_cls(signals=Signals(legal_press_vip=True)))
    assert r.decision is Decision.NEEDS_MANAGER_APPROVAL
    assert "manager reads" in r.reason


def test_fill_in_blanks_and_markdown_links_are_not_placeholders():
    from aflalo_cs.verify import PLACEHOLDER_RE
    assert PLACEHOLDER_RE.findall("The updated ship date is ______.") == []
    assert PLACEHOLDER_RE.findall("start it through [our returns portal](https://returns.aflalonyc.com)") == []
    assert PLACEHOLDER_RE.findall("your order shipped on [date]") == ["[date]"]
