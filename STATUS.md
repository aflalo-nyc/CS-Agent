# CS Agent: status and next steps

Owner: Gloria Melidoni (handed over by Sanskriti Akhoury, 2026-10-02)
Last checked: 2026-10-05

## State: live since 2026-10-05, no live draft yet

| | |
|---|---|
| Service | Railway `aflalo-cs-agent` → `agent`, https://agent-production-0a9f.up.railway.app/health answers `ok` |
| What it does every 10 min | Reads the last 30 days of aflalo@ (read-only, ~195 messages, takes ~4 min), drafts only mail received after the go-live cutoff, then pushes to Airtable. |
| Go-live cutoff | 2026-10-05 19:12:45 UTC, stamped on the first real cycle. The 19:13 and 19:27 cycles found no new mail, so no live draft exists yet. |
| Old rows | The 416 rows already in CS Drafts are Sanskriti's September tests and have no Open in Gmail link. Live rows are the ones with a link. |
| Tests | 173 pass (`python -m pytest tests -q`, no keys needed) |
| Shopify | Live, read-only (app `sanskriti-cs`) |
| Airtable | base `app86LogrXQQ1hno9`: CS Drafts, CS Threads, CS KPI Summary |
| Sends email? | Never. No send permission, by design. Drafts go to Airtable; the Open in Gmail button puts one into the Gmail thread when a person clicks it. |

## Done 2026-10-05

1. **App Password** made on aflalo@aflalonyc.com (the handoff call called this "the 2FA code").
2. **Set on Railway** as `agent` → Variables → `AFLALO_IMAP_PASSWORD`. If it is ever replaced, the
   name must match exactly, because the agent ignores any other name.
3. **First cycle ran** at 19:13 UTC: imported 195 messages, pushed 83 conversations to CS Threads
   and the KPI rows, no errors.

## To finish

1. **Check one draft lands in the right thread.** When the first CS Drafts row with an Open in Gmail
   link appears, click it and confirm the draft sits inside the customer's thread in aflalo@'s Gmail.
   Threading has only been tested against a fake mailbox. Clicking twice does not make two drafts.
2. **Review period.** The CS team reads every draft in Airtable before sending, and writes feedback in
   the Comments + Feedback columns. Decide how long this lasts before anyone considers putting drafts
   straight into Gmail (steps in `docs/AGENT.html`).

## Open, not blocking go-live

- **Repo is public.** `knowledge/` holds internal policy notes and sizing specs. Make it private
  (GitHub → Settings → Danger Zone), then confirm a Railway redeploy still builds.
- **Pushes don't deploy.** After any change, Railway → `agent` → Deploy by hand, or reconnect the
  GitHub source under Settings → Source with the account that owns the Railway project.
- **Data the drafts still lack:**
  - Garment specs for Tavi Pant and Sagan jeans (Production)
  - Preorder terms in writing
  - Sign-off ruling: the deck says "Warmly, Eva", the team writes "Best, Eva"
  - Shopify scope `read_inventory_transfers` for restock dates
  - International price quotes: the three questions in `PRICING_QUESTIONS.md` (are duties in the
    checkout price, the shipping rate card, and the wording of a quote)
- **`learn` runs from a laptop only**, because it writes `knowledge/lessons.md` into the repo.
- The two policy conflicts in the README were resolved 2026-08-26 from the published refund policy
  (see `PROJECT.md` → Standing gaps).

## Where things are

Full map, settings and "where to change what": `docs/AGENT.html`. How it works: `PROJECT.md`.
