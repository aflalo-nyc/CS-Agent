# CS Agent: status and next steps

Owner: Gloria Melidoni (handed over by Sanskriti Akhoury, 2026-10-02)
Last checked: 2026-10-05

## State: deployed, but doing nothing yet

| | |
|---|---|
| Service | Railway `aflalo-cs-agent` → `agent`, https://agent-production-0a9f.up.railway.app/health answers `ok` |
| What it does every 10 min | Logs `AFLALO_IMAP_PASSWORD not set — cycle skipped`. It has not imported one email or written one draft. |
| Tests | 173 pass (`python -m pytest tests -q`, no keys needed) |
| Shopify | Live, read-only (app `sanskriti-cs`) |
| Airtable | base `app86LogrXQQ1hno9`: CS Drafts, CS Threads, CS KPI Summary |
| Sends email? | Never. No send permission, by design. Drafts go to Airtable; the Open in Gmail button puts one into the Gmail thread when a person clicks it. |

The handoff call called this "the 2FA code". The thing actually missing is a **Gmail App Password**
for aflalo@aflalonyc.com. Google only lets you make one after signing in to that account with its
2-step code, so it has to be done by whoever holds that account's 2-step phone.

## To finish

1. **Make the App Password.** Sign in as aflalo@aflalonyc.com → https://myaccount.google.com/apppasswords
   → name it `cs-agent` → copy the 16 letters (shown once).
2. **Put it on Railway.** `aflalo-cs-agent` → `agent` → Variables → `AFLALO_IMAP_PASSWORD`. The
   service redeploys on its own.
3. **Check it ran.** Within 10 minutes the deploy log shows `agent: imported … messages (read-only)`
   and new rows appear in Airtable → CS Drafts.
   Only mail that arrives *after* this first run is drafted. The cutoff is stamped on the first real
   cycle, so the backlog is left alone.
4. **Check one draft lands in the right thread.** Click Open in Gmail on one CS Drafts row and confirm
   the draft sits inside the customer's thread in Gmail. Threading has only been tested against a fake
   mailbox.
5. **Review period.** The CS team reads every draft in Airtable before sending, and writes feedback in
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
