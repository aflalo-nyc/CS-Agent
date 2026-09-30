"""The standing review loop: reviewer comments in Airtable become agent behavior.

The team leaves feedback where they already work — the 'Comments + Feedback' column on
CS Drafts. `aflalo-cs learn` pulls every comment, has the model distill the GENERAL
principle behind each one (never the one-off fix), and writes the style principles into
knowledge/lessons.md, which every subsequent draft receives as binding rules.

Two kinds of feedback come out of a review, and they are deliberately handled differently:

- STYLE — how replies should read (scope, tone, format). Safe to bind into the prompt
  automatically; the voice lint and verifier still backstop everything.
- PROCESS / POLICY — who gets cc'd, what a remedy is, where a cutoff sits. These change
  what the company DOES, so they are printed as proposals for a human to encode in
  categories.yaml / policy_facts.yaml, never silently applied from a comment.

lessons.md is versioned in git: every review round is a diff someone can read and revert.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from . import knowledge
from .llm import LLM, cached_system

FEEDBACK_FIELD = "Comments + Feedback"

DISTILL_SYSTEM = """You maintain the style rulebook for a customer-service drafting agent.
You receive reviewer comments left on individual drafts, plus the current rulebook.

For each comment, extract the GENERAL principle the reviewer is applying — the rule that
would have prevented this comment on ANY email, not just the one it was left on. Merge with
the existing rulebook: keep every existing lesson that still stands, fold duplicates
together, never contradict a newer instruction with an older one.

Classify each principle:
- style: how replies should read (scope, brevity, format, phrasing). These become binding.
- process: what the company does (who is cc'd, remedies, cutoffs, routing). NOT yours to
  apply — return them separately as proposals for a human to encode.

Write style lessons as short imperatives a drafter can obey, each with provenance:
"(<reviewer>, <date>)". No numbering, no headers inside lessons."""

DISTILL_SCHEMA = {
    "type": "object",
    "properties": {
        "style_lessons": {"type": "array", "items": {"type": "string"}},
        "process_proposals": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["style_lessons", "process_proposals"],
    "additionalProperties": False,
}


def pull_comments(airtable) -> list[dict]:
    """Every row whose feedback column has content: comment + the context it was left on."""
    out, offset = [], None
    while True:
        params: dict = {
            "fields[]": [FEEDBACK_FIELD, "Category", "Subject", "Draft"],
            "pageSize": 100,
            "filterByFormula": f"NOT({{{FEEDBACK_FIELD}}} = '')",
        }
        if offset:
            params["offset"] = offset
        data = airtable._call("GET", airtable.table, params=params)
        for rec in data.get("records", []):
            f = rec.get("fields", {})
            comment = (f.get(FEEDBACK_FIELD) or "").strip()
            if comment:
                out.append(
                    {
                        "comment": comment,
                        "category": (f.get("Category") or {}).get("name")
                        if isinstance(f.get("Category"), dict)
                        else f.get("Category"),
                        "subject": f.get("Subject") or "",
                        "draft": (f.get("Draft") or "")[:600],
                    }
                )
        offset = data.get("offset")
        if not offset:
            return out


def distill(llm: LLM, comments: list[dict]) -> dict:
    current = knowledge.lessons() or "(empty — first review round)"
    blocks = []
    for c in comments:
        blocks.append(
            f"CATEGORY: {c['category']}\nSUBJECT: {c['subject']}\n"
            f"THE DRAFT REVIEWED:\n{c['draft']}\nREVIEWER COMMENT:\n{c['comment']}"
        )
    user = (
        "CURRENT RULEBOOK:\n" + current + "\n\n"
        "REVIEWER COMMENTS (one block per draft):\n\n" + "\n\n---\n\n".join(blocks)
    )
    return llm.structured(
        system=cached_system(DISTILL_SYSTEM), user=user,
        schema=DISTILL_SCHEMA, effort="high", max_tokens=4096,
    )


def write_lessons(style_lessons: list[str], path: Path | None = None) -> Path:
    path = path or knowledge.KNOWLEDGE_DIR / "lessons.md"
    lines = [
        "LESSONS FROM TEAM REVIEW — binding on every draft, newest review last.",
        f"(Maintained by `aflalo-cs learn` from the Airtable '{FEEDBACK_FIELD}' column; "
        f"last updated {date.today().isoformat()}.)",
        "",
    ]
    for lesson in style_lessons:
        clean = re.sub(r"\s+", " ", lesson).strip().rstrip(".") + "."
        lines.append(f"- {clean}")
    path.write_text("\n".join(lines) + "\n")
    return path
