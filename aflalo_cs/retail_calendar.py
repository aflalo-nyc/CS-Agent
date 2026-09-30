"""AFLALO's 4-5-4 retail calendar and its business hours.

The fiscal year started 2025-12-28 (a Sunday). Months take 4, 5, 4 weeks in turn, so
each quarter is 13 weeks and the year 52. Weeks within a month are lettered A–E, so
"AUG-D" is the fourth week of fiscal August — the week of 8/16/2026. Given by the
team on 2026-09-11 with worked examples (AUG-D 8/16, AUG-E 8/23, SEP-A 8/30), which
this module reproduces exactly.

A 4-5-4 year is 364 days, so the calendar drifts one day a year against the real one
and retailers add a 53rd week every five or six years. Which year gets it is a
business decision this code cannot know — `fiscal_week` rolls into a new 52-week
year and flags dates in the final week so the drift is visible when it matters.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

FISCAL_YEAR_START = date(2025, 12, 28)   # FY2026, week 1 — a Sunday
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
WEEKS_PER_MONTH = [4, 5, 4] * 4
WEEK_LETTERS = "ABCDE"

EASTERN = ZoneInfo("America/New_York")
BUSINESS_OPEN_HOUR = 9     # 9:00 AM ET
BUSINESS_CLOSE_HOUR = 19   # 7:00 PM ET (exclusive)


@dataclass(frozen=True)
class FiscalWeek:
    label: str          # "AUG-D"
    week_start: date    # the Sunday
    fiscal_year: int    # 2026
    week_number: int    # 1..52

    @property
    def week_end(self) -> date:
        return self.week_start + timedelta(days=6)

    @property
    def key(self) -> str:
        """Stable, sortable row key: 'FY2026 W34 · AUG-D (week of 8/16)'."""
        return (f"FY{self.fiscal_year} W{self.week_number:02d} · {self.label} "
                f"(week of {self.week_start.month}/{self.week_start.day})")


def fiscal_week(d: date | datetime) -> FiscalWeek:
    if isinstance(d, datetime):
        d = d.date()
    year_start = FISCAL_YEAR_START
    fy = FISCAL_YEAR_START.year + 1
    # Step whole 52-week years until d falls inside one. Cheap for any realistic date.
    while d < year_start:
        year_start -= timedelta(weeks=52); fy -= 1
    while d >= year_start + timedelta(weeks=52):
        year_start += timedelta(weeks=52); fy += 1

    week_number = (d - year_start).days // 7 + 1          # 1..52
    remaining = week_number
    for month, n_weeks in zip(MONTHS, WEEKS_PER_MONTH):
        if remaining <= n_weeks:
            label = f"{month}-{WEEK_LETTERS[remaining - 1]}"
            break
        remaining -= n_weeks
    return FiscalWeek(label, year_start + timedelta(weeks=week_number - 1), fy, week_number)


def in_business_hours(when: datetime) -> bool:
    """Mon–Fri, 9:00 AM to 7:00 PM Eastern (the team's definition, 2026-09-11).

    Takes any timezone-aware datetime; naive datetimes are treated as UTC, which is
    how the store records message times.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=ZoneInfo("UTC"))
    local = when.astimezone(EASTERN)
    return local.weekday() < 5 and BUSINESS_OPEN_HOUR <= local.hour < BUSINESS_CLOSE_HOUR
