"""Tariff holiday calendar: turns the rules in the tariff config into concrete Off-Peak dates."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th `weekday` of a month; n = -1 is the last one."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7 + 7 * (-n - 1))


def holiday_date(rule: dict, year: int) -> date:
    if rule["type"] == "fixed":
        return date(year, rule["month"], rule["day"])
    if rule["type"] == "nth_weekday":
        return nth_weekday(year, rule["month"], WEEKDAYS[rule["weekday"]], rule["n"])
    raise ValueError(f"unknown holiday rule type {rule['type']!r}")


def offpeak_holidays(rules: list[dict], years: range | list[int]) -> dict[date, str]:
    """Map of every date that is an Off-Peak holiday (actual or observed) in `years`.

    Neighbouring years are evaluated too, so e.g. a Saturday Jan 1 correctly makes the
    preceding Dec 31 observed.
    """
    years = list(years)
    if not years:
        return {}
    out: dict[date, str] = {}
    for y in range(min(years) - 1, max(years) + 2):
        for rule in rules:
            d = holiday_date(rule, y)
            out.setdefault(d, rule["name"])
            if rule.get("observance", "none") == "weekend_shift":
                if d.weekday() == 5:
                    out.setdefault(d - timedelta(days=1), f"{rule['name']} (observed)")
                elif d.weekday() == 6:
                    out.setdefault(d + timedelta(days=1), f"{rule['name']} (observed)")
    wanted = set(years)
    return {d: name for d, name in sorted(out.items()) if d.year in wanted}
