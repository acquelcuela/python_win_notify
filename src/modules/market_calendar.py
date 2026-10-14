"""Tokyo Stock Exchange calendar: weekends, Japanese national holidays
(jpholiday) and the year-end/new-year closure (12/31 - 1/3).

Used by main.py so that a market holiday on a weekday is scheduled like a
Saturday (2026-10-14): weekday-only stock slots are skipped, "all"/"weekend"
slots still run. Before this, holidays such as 10/12 ran the full stock
pipeline - candidates were logged for a day with no trading and then scored
with the next session's moves.
"""
from __future__ import annotations

from datetime import date, datetime

import jpholiday

WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def is_market_holiday(day: date) -> bool:
    """True when the TSE is closed on a weekday (national holiday or 12/31-1/3)."""
    if day.weekday() >= 5:
        return False
    if (day.month == 12 and day.day == 31) or (day.month == 1 and day.day <= 3):
        return True
    return bool(jpholiday.is_holiday(day))


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and not is_market_holiday(day)


def schedule_day_key(now: datetime) -> str:
    """Weekday name for schedule matching; a market holiday counts as "sat"."""
    if is_market_holiday(now.date()):
        return "sat"
    return WEEKDAY_NAMES[now.weekday()]
