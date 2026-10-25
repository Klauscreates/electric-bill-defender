"""Household configuration: confirmed tariff, account eligibility, billing cycles and bills."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yaml

from ebd import LOCAL_TZ
from ebd.tariff import Tariff
from ebd.util import ConfigError, UnsupportedAccount, as_date, sha256_file

# Schedules a household might be on that this version deliberately does not analyze.
KNOWN_UNSUPPORTED = {
    "r-2": "Saver Choice Plus (R-2) bills a monthly demand charge ($/kW).",
    "saver choice plus": "Saver Choice Plus (R-2) bills a monthly demand charge ($/kW).",
    "r-3": "R-3 / R3-47 bills a monthly demand charge ($/kW).",
    "r3-47": "R-3 / R3-47 bills a monthly demand charge ($/kW).",
    "saver choice max": "Saver Choice Max bills a monthly demand charge ($/kW).",
    "r-1": "R-1 is a fixed (non-time-of-use) energy plan; shifting load in time changes nothing.",
    "premier choice": "Premier Choice is a non-time-of-use plan; shifting load in time changes nothing.",
    "r-ev-overnight": "The EV Overnight plan has different time periods; it is not configured yet.",
}

REQUIRED_FLAGS = ("solar", "battery", "demand_charge")


@dataclass
class BillingCycle:
    cycle_id: str
    start: date          # first day of service, inclusive
    end: date            # last day of service, inclusive
    billing_month: str   # YYYY-MM label that sets the season
    season: str
    bill: dict | None
    notes: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def start_ts(self) -> pd.Timestamp:
        return pd.Timestamp(self.start).tz_localize(LOCAL_TZ)

    @property
    def end_ts(self) -> pd.Timestamp:
        """Exclusive end instant: midnight after the last day of service."""
        return pd.Timestamp(self.end + timedelta(days=1)).tz_localize(LOCAL_TZ)


@dataclass
class Household:
    household_id: str
    synthetic: bool
    rate_schedule: str
    confirmed_on: str
    cycles: list[BillingCycle]
    path: Path
    sha256: str


def check_account(account: dict, tariff: Tariff) -> None:
    missing = [k for k in REQUIRED_FLAGS if k not in account]
    if missing:
        raise ConfigError(
            f"account must state {', '.join(missing)} explicitly (true/false) so eligibility is confirmed, not assumed"
        )
    reasons = []
    if account["demand_charge"]:
        reasons.append("Demand-charge plans are not supported: the bill depends on the highest kW interval, which this version does not model.")
    if account["solar"]:
        reasons.append("Solar households are not supported: exports, net billing (RCP/EPR riders) and the Grid Access Charge are not modeled.")
    if account["battery"]:
        reasons.append("Battery households are not supported: battery dispatch already shifts load and would be double counted.")
    sched = str(account.get("rate_schedule", "")).strip()
    if not sched:
        reasons.append("account.rate_schedule is required; copy it from your APS bill.")
    elif not tariff.matches_schedule(sched):
        why = KNOWN_UNSUPPORTED.get(sched.lower(), "Only APS TOU-E (Time-of-Use 4pm-7pm Weekdays, no demand charge) is supported.")
        reasons.append(f"Rate schedule {sched!r} is not supported. {why}")
    if reasons:
        raise UnsupportedAccount("\n".join(reasons))


def load_household(path: str | Path, tariff: Tariff) -> Household:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read household config {path}: {e}") from e

    account = raw.get("account") or {}
    check_account(account, tariff)

    cycles = []
    for i, c in enumerate(raw.get("billing_cycles") or []):
        where = f"billing_cycles[{i}]"
        if "start" not in c or "end" not in c:
            raise ConfigError(f"{where}: start and end are required")
        start = as_date(c["start"], f"{where}.start")
        end = as_date(c["end"], f"{where}.end")
        if end < start:
            raise ConfigError(f"{where}: end {end} is before start {start}")
        bm = str(c.get("billing_month") or "")
        if not bm:
            raise ConfigError(f"{where}: billing_month (YYYY-MM, as printed on the bill) is required; it sets the season")
        try:
            month = int(bm.split("-")[1])
        except (IndexError, ValueError):
            raise ConfigError(f"{where}: billing_month must look like 2025-06, got {bm!r}") from None
        cycle = BillingCycle(
            cycle_id=str(c.get("id") or bm),
            start=start,
            end=end,
            billing_month=bm,
            season=tariff.season_for_billing_month(month),
            bill=c.get("bill"),
            notes=str(c.get("notes") or ""),
        )
        if "bill_days" in c and int(c["bill_days"]) != cycle.days:
            hint = ""
            if int(c["bill_days"]) == cycle.days - 1:
                hint = " If your bill counts the end date as the next meter read, set end to the day before."
            raise ConfigError(
                f"{where}: start..end inclusive is {cycle.days} days but bill_days is {c['bill_days']}.{hint}"
            )
        cycles.append(cycle)

    if not cycles:
        raise ConfigError("household config has no billing_cycles; copy the service periods from your bills")
    cycles.sort(key=lambda c: c.start)
    ids = [c.cycle_id for c in cycles]
    if len(set(ids)) != len(ids):
        raise ConfigError("billing cycle ids must be unique")
    for a, b in zip(cycles, cycles[1:]):
        if b.start <= a.end:
            raise ConfigError(f"billing cycles {a.cycle_id} and {b.cycle_id} overlap")

    return Household(
        household_id=str(raw.get("household_id") or path.stem),
        synthetic=bool(raw.get("synthetic", False)),
        rate_schedule=str(account["rate_schedule"]),
        confirmed_on=str(account.get("confirmed_on", "")),
        cycles=cycles,
        path=path,
        sha256=sha256_file(path),
    )
