"""Command line entry point.

    python -m aflalo_cs.cli run                 # mock inbox, fixtures, real model
    python -m aflalo_cs.cli run --live-inbox    # real Gmail (draft-only scopes)
    python -m aflalo_cs.cli report              # what the run decided and why
    python -m aflalo_cs.cli doctor              # what is actually wired up right now
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import config
from .llm import AnthropicLLM
from .models import Decision
from .pipeline import Pipeline
from .store import Store

BULLET = {"draft": "\033[32m✓\033[0m", "needs-manager-approval": "\033[33m⚑\033[0m",
          "draft-unverified": "\033[35m!\033[0m", "needs-human": "\033[31m→\033[0m"}


def _probe_anthropic() -> str:
    """Actually call the API. Credentials resolving is not the same as the API working —
    a valid login against an org with no credits looks identical until you try."""
    try:
        import anthropic

        anthropic.Anthropic().messages.create(
            model="claude-opus-5", max_tokens=1, messages=[{"role": "user", "content": "ok"}]
        )
        return "\033[32mreachable\033[0m"
    except Exception as exc:  # noqa: BLE001 — doctor reports, never raises
        msg = str(exc)
        if "credit balance is too low" in msg:
            return "\033[31mauth OK but org has NO API CREDITS\033[0m — add credits, or log in to an org that has them"
        if "authentication" in msg.lower() or "401" in msg:
            return "\033[31mcredentials rejected\033[0m — re-run `ant auth login`"
        return f"\033[31munreachable\033[0m — {msg[:110]}"


def cmd_doctor(args) -> int:
    shop, shop_mode = config.get_shopify()
    print("Integration status")
    cred = config.anthropic_credential()
    if not cred:
        print("  Anthropic API  : NOT configured (set ANTHROPIC_API_KEY or run `ant auth login`)")
    elif args.probe:
        print(f"  Anthropic API  : {cred} -> {_probe_anthropic()}")
    else:
        print(f"  Anthropic API  : credentials found via {cred} (run with --probe to test the call)")
    live = shop_mode.startswith("live")
    if live and args.probe:
        print(f"  Shopify        : {shop_mode} -> {shop.probe()}")
    else:
        print("  Shopify        :", shop_mode + ("" if live else f"  ({config.shopify_hint()})"))
    print("  Gmail          : mock by default; --live-inbox uses OAuth (read/label/draft scopes, no send)")
    print("  Store          :", config.DB_PATH)
    from . import knowledge

    cats = knowledge.categories()
    tiers: dict[str, int] = {}
    for name in cats:
        tiers[knowledge.tier(name)] = tiers.get(knowledge.tier(name), 0) + 1
    print(f"\nTaxonomy: {len(cats)} categories — " + ", ".join(f"{v} {k}" for k, v in sorted(tiers.items())))
    return 0


def cmd_run(args) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    if not config.have_anthropic_key():
        print("ANTHROPIC_API_KEY is not set — run `python -m aflalo_cs.cli doctor`.", file=sys.stderr)
        return 2

    store = Store(config.DB_PATH)
    if args.from_store:
        from .gmail_client import StoredMailbox

        mailbox, mail_mode = StoredMailbox(store=store), "local store (read-only import)"
    else:
        mailbox, mail_mode = config.get_mailbox(args.live_inbox)
    shop, shop_mode = config.get_shopify()
    pipe = Pipeline(
        mailbox=mailbox,
        llm=AnthropicLLM(),
        shop=shop,
        store=store,
        verify_with_model=not args.no_model_verify,
        shadow=args.shadow,
    )

    mode = "\033[33mSHADOW — nothing will be written\033[0m" if args.shadow else "live (drafts + labels)"
    print(f"inbox={mail_mode}  shopify={shop_mode}  mode={mode}\n")
    if args.reprocess and not args.from_store:
        print("--reprocess only works with --from-store — reprocessing a live mailbox would "
              "write a second Gmail draft for every email.")
        return 2
    outcomes = pipe.run(limit=args.limit, reprocess=args.reprocess)

    for o in outcomes:
        mark = BULLET.get(o.decision.value, "?")
        print(f"{mark} {o.message_id}  [{o.category or '-'}]  {o.decision.value}")
        print(f"    why: {o.reason}")
        if args.show_drafts and o.draft_text:
            prefix = "    withheld draft" if o.withheld else "    draft"
            body = "\n      ".join(o.draft_text.splitlines())
            print(f"{prefix}:\n      {body}")
        print()

    counts = {d.value: sum(1 for o in outcomes if o.decision is d) for d in Decision}
    total = len(outcomes) or 1
    print(f"{len(outcomes)} processed — " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    print(f"auto-draft rate: {counts['draft'] / total:.0%}")
    return 0


def cmd_report(_args) -> int:
    store = Store(config.DB_PATH)
    print("Actions:", store.stats() or "(nothing processed yet)")
    print("\nEscalation reasons (most common first):")
    for by, reason, n in store.escalation_reasons()[:15]:
        print(f"  {n:>3}  [{by}] {reason}")
    print("\nCoverage gaps — categories that keep needing a human (add these next):")
    for cat, n in store.coverage_gaps()[:10]:
        print(f"  {n:>3}  {cat}")

    k = store.kpi_summary()
    if k["threads"]:
        h = lambda v: "—" if v is None else f"{v:>7.1f}h"  # noqa: E731

        def sla(block, label):
            print(
                f"\nSLA ({label}) — {block['threads']} conversations: {block['answered']} answered, "
                f"{block['resolved']} resolved, {block['awaiting_first_reply']} still waiting"
            )
            print(f"  first response   median {h(block['first_response_median_h'])}   p90 {h(block['first_response_p90_h'])}")
            print(f"  resolution       median {h(block['resolution_median_h'])}   p90 {h(block['resolution_p90_h'])}")
            if block["oldest_unanswered_h"]:
                print(f"  oldest unanswered thread: {block['oldest_unanswered_h'] / 24:.1f} days")

        # Customers first — that's the SLA. The all-mail block includes vendor/recruiting
        # threads, where a fast reply to a sales pitch would flatter the number.
        sla(k["customers"], "customers")
        if k["customers"]["threads"] != k["threads"]:
            sla(k, "all mail")
    return 0


def cmd_learn(_args) -> int:
    """Pull reviewer comments from Airtable, distill the general principles, bind the
    style ones into knowledge/lessons.md, print process changes as proposals."""
    import os

    from . import feedback
    from .airtable import Airtable
    from .llm import AnthropicLLM

    token, base = os.environ.get("AIRTABLE_TOKEN"), os.environ.get("AIRTABLE_BASE")
    if not (token and base):
        print("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
        return 2
    comments = feedback.pull_comments(Airtable(token, base))
    if not comments:
        print("No reviewer comments found — nothing to learn.")
        return 0
    print(f"{len(comments)} reviewer comment(s) pulled. Distilling…")
    result = feedback.distill(AnthropicLLM(), comments)
    path = feedback.write_lessons(result["style_lessons"])
    print(f"\n{len(result['style_lessons'])} binding style lessons -> {path}")
    for l in result["style_lessons"]:
        print(f"  - {l}")
    if result["process_proposals"]:
        print("\nPROCESS/POLICY proposals — a human encodes these (categories.yaml / policy_facts.yaml):")
        for pr in result["process_proposals"]:
            print(f"  * {pr}")
    print("\nRe-run drafting to apply: aflalo-cs run --from-store --reprocess")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aflalo-cs")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="process the inbox")
    r.add_argument("--live-inbox", action="store_true", help="use real Gmail instead of the mock")
    r.add_argument("--from-store", action="store_true", help="draft against the read-only inbox import (never touches Gmail)")
    r.add_argument("--limit", type=int, default=50)
    r.add_argument("--show-drafts", action="store_true")
    r.add_argument("--no-model-verify", action="store_true", help="layer 1 grounding only")
    r.add_argument("--shadow", action="store_true", help="classify and log only — write NOTHING to Gmail")
    r.add_argument("--reprocess", action="store_true",
                   help="re-draft messages already processed (from-store only) — for when the pipeline got smarter")
    r.set_defaults(func=cmd_run)

    sub.add_parser("report", help="audit trail summary").set_defaults(func=cmd_report)
    sub.add_parser(
        "learn",
        help="absorb reviewer comments from Airtable into binding draft lessons",
    ).set_defaults(func=cmd_learn)
    d = sub.add_parser("doctor", help="show what is wired up")
    d.add_argument("--probe", action="store_true", help="make a real 1-token API call to test it")
    d.set_defaults(func=cmd_doctor)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
