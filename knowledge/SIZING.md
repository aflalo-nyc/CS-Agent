# Sizing — what Production's folder actually gives us

Source: [Production's size-by-style folder](https://drive.google.com/drive/folders/1genAmeiLls_iITRbGPVU6TAxDcOOwo7D),
imported 2026-08-21. Structured into `sizing.yaml`; read this before using it.

## The one thing to understand first

**These are finished-garment measurements, not body measurements.** The spec says what the
garment measures flat, not what body it fits. That distinction decides what a reply may say:

| | |
| --- | --- |
| ✅ "The Aire Dress in a 4 measures 28½″ at the chest and 28⅛″ at the waist." | A fact from a spec. Checkable. |
| ❌ "Based on that, you'd want a 6." | Needs her measurements, an ease assumption, and a guess at how she likes things to fit. |

`usage_policy.may_recommend_a_size` is `false` and `knowledge.may_recommend_a_size()`
enforces it. A wrong size recommendation costs a return, a refund, and her confidence —
three things a measurement quote cannot cost. Give her the numbers and let her decide.

Always state the unit, and always say it's the garment.

## What's usable

Eight styles, normalised to inches. Styles that came in centimetres keep their original
values under `measurements_cm` so nothing is lost to rounding.

| Style | Spec | Sizes | Bust/chest | Waist | Hip |
| --- | --- | --- | :-: | :-: | :-: |
| Aire Dress (DD026) | released graded | 0–10 | ✅ | ✅ | ✅ |
| Bexley Dress (DD028) | released graded | 0–10 | ✅ | — | — |
| Nourin Dress (DD025) | graded | XS–XL | ✅¹ | ✅ | ✅¹ |
| Tibet Top (DD031) | graded | XS–L | — | — | — |
| Safira Top (TT017) | graded | XS–XL | ✅¹ | — | — |
| Dara | scheda misure (cm) | XXS–XL | ✅ | ✅ | ✅ |
| Livia | scheda misure (cm) | XXS–XL | ✅ | ✅ | ✅ |
| Viretta | scheda misure (cm) | XXS–XL | — | — | ⚠️ excluded |

¹ recorded as a **width**, not a circumference. Don't compare it against a circumference.

## Three size systems, two units

The folder mixes them, and it isn't a mistake to be cleaned up — it reflects how the
garments were developed:

- **0–10 numeric** — US graded specs (Aire, Bexley)
- **XS–XL** — US graded specs (Nourin, Tibet, Safira)
- **XXS–XL** — Italian *schede misure*, in centimetres (Dara, Livia, Viretta)

So "size M" is not answerable for the Aire Dress and "size 4" is not answerable for Dara.
`measurements_for()` returns `{}` rather than reaching for the nearest neighbour — an empty
result routes the email to a human, which is right. A neighbouring size would be a
plausible-looking, checkable-looking, wrong number, and that is the worst kind.

## What is withheld, and why

Three files are in `withheld:` and unreachable from `sizing_styles()`. Each would produce a
confidently wrong answer:

- **Aksel Pant** — the file holds **grading deltas only** (`-3, 0, +4 …`), not measurements.
  There's no base-size spec here to apply them to, so no actual waist or inseam exists in
  it. *Ask Production for the base size 4 spec.*
- **Audra Jacket** — a development workbook, not a released spec: proto and PPS columns sit
  beside the graded ones and a blank BLAZER template is appended to the same sheet. Its
  graded chest column reads 21–25″, which is a half measurement or an error, not a jacket
  chest. *Ask for the released graded spec.*
- **Valeo / Elwen Bomber** — **the file name and its contents name two different garments.**
  The file is `1020 ELWEN BOMBER`; the sheet inside says `VALEO BOMBER`. Quoting it risks
  sending a customer another garment's measurements. *Ask which style this is.*

Viretta is available but its hip row is excluded: it reads 55.7–80.7 cm across the range,
too small to be a body circumference, so it's a half measurement or a different point.

## Coverage

**Eight styles.** The catalogue is much larger, and this folder is a start rather than a
reference. Every style not in it produces an empty lookup and a human-tier escalation, which
is correct but doesn't scale. The ask to Production is a released graded spec per style,
ideally in one system.

## Guarding the transcription

The specs were hand-transcribed from PDFs and spreadsheets, so
`test_sizing_measurements_grow_monotonically_with_size` asserts that every point of measure
on every style increases (or holds) as the size goes up. A transposed digit breaks
monotonicity and fails the suite. That check exists because a wrong number here is invisible
by eye and would be quoted to a customer as fact.
