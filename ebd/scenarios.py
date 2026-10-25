"""Baseline reconstruction and load-shift scenarios.

Every scenario keeps each interval in its own billing cycle, season and tariff version,
and only moves energy within the same local day, so daily kWh is conserved exactly and
each scenario is priced under the same tariff as its baseline.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ebd.household import Household
from ebd.tariff import Tariff, classify, rates_for
from ebd.util import hhmm_to_minutes

SHIFT_SHARES = (0.10, 0.25, 0.50)
DEST_WINDOW = ("19:00", "23:00")
CEILING = "ceiling"


@dataclass
class ScenarioSettings:
    shares: tuple[float, ...] = SHIFT_SHARES
    dest_window: tuple[str, str] = DEST_WINDOW

    @classmethod
    def for_tariff(cls, tariff: Tariff, shares: tuple[float, ...] = SHIFT_SHARES) -> "ScenarioSettings":
        return cls(shares=shares, dest_window=tariff.shift_destination)

    @property
    def dest_minutes(self) -> tuple[int, int]:
        return hhmm_to_minutes(self.dest_window[0], "dest start"), hhmm_to_minutes(self.dest_window[1], "dest end")

    def keys(self) -> list[str]:
        return [shift_key(s) for s in self.shares] + [CEILING]

    def describe(self) -> dict:
        return {
            "shift_scenarios": [
                {"key": shift_key(s), "share_of_weekday_on_peak_kwh": s,
                 "destination": f"same day {self.dest_window[0]}-{self.dest_window[1]}, spread evenly by time",
                 "label": "illustrative sensitivity scenario"} for s in self.shares
            ],
            "ceiling": {"key": CEILING, "share_of_on_peak_kwh": 1.0,
                        "destination": "same day, the lowest-priced non-on-peak hours (super off-peak where the tariff has it, otherwise all off-peak hours), spread evenly by time",
                        "label": "theoretical ceiling, physically unvalidated"},
        }


def shift_key(share: float) -> str:
    return f"shift_{round(share * 100):d}"


def annotate(rows: pd.DataFrame, tariff: Tariff) -> pd.DataFrame:
    """Baseline pricing for valid rows: TOU period, rate and energy charge per interval."""
    df = rows[rows.valid].copy().sort_values("start", kind="stable").reset_index(drop=True)
    df["period"] = classify(tariff, df.start, df.season, df.version_id)
    df["rate_per_kwh"] = rates_for(tariff, df.season, df.period, df.version_id)
    df["baseline_cost"] = df.kwh * df.rate_per_kwh
    df["local_date"] = df.start.dt.date
    df["start_minute"] = df.start.dt.hour * 60 + df.start.dt.minute + df.start.dt.second / 60
    return df


def _spread(df: pd.DataFrame, day_total: pd.Series, weight: pd.Series) -> pd.Series:
    """Distribute each day's total over that day's intervals in proportion to `weight`."""
    wsum = weight.groupby(df.local_date).transform("sum")
    need = day_total > 0
    if (need & (wsum <= 0)).any():
        bad = sorted(set(df.local_date[need & (wsum <= 0)]))
        raise ValueError(f"no destination intervals on {bad[:5]}")
    return (day_total * weight / wsum.where(wsum > 0, 1)).where(wsum > 0, 0.0)


def run_scenarios(df: pd.DataFrame, settings: ScenarioSettings) -> pd.DataFrame:
    out = df.copy()
    on = out.period == "on_peak"
    d0, d1 = settings.dest_minutes
    end_minute = out.start_minute + out.minutes
    overlap = (np.minimum(end_minute, d1) - np.maximum(out.start_minute, d0)).clip(lower=0)
    if ((overlap > 0) & on).any():
        raise ValueError("the destination window overlaps on-peak hours; that is not a shift")
    out["in_dest_window"] = overlap > 0

    for share in settings.shares:
        k = shift_key(share)
        moved = (out.kwh * share).where(on, 0.0)
        day_moved = moved.groupby(out.local_date).transform("sum")
        added = _spread(out, day_moved, overlap)
        _store(out, k, moved, added)

    # Theoretical ceiling: all on-peak kWh to the day's cheapest eligible hours.
    moved = out.kwh.where(on, 0.0)
    day_moved = moved.groupby(out.local_date).transform("sum")
    elig_rate = out.rate_per_kwh.where(~on)
    day_min = elig_rate.groupby(out.local_date).transform("min")
    dest = ~on & np.isclose(out.rate_per_kwh, day_min)
    out["ceiling_dest"] = dest
    added = _spread(out, day_moved, out.minutes.where(dest, 0.0))
    _store(out, CEILING, moved, added)
    return out


def _store(out: pd.DataFrame, k: str, moved: pd.Series, added: pd.Series) -> None:
    out[f"{k}_kwh_out"] = moved
    out[f"{k}_kwh_in"] = added
    out[f"{k}_kwh"] = out.kwh - moved + added
    out[f"{k}_cost"] = out[f"{k}_kwh"] * out.rate_per_kwh
    out[f"{k}_savings"] = out.baseline_cost - out[f"{k}_cost"]


def _dest_mask(df: pd.DataFrame, k: str) -> pd.Series:
    return df.ceiling_dest if k == CEILING else df.in_dest_window


def cycle_summary(df: pd.DataFrame, cycles: pd.DataFrame, settings: ScenarioSettings) -> pd.DataFrame:
    rows = []
    complete = cycles[cycles.status == "complete"]
    for c in complete.itertuples(index=False):
        g = df[df.cycle_id == c.cycle_id]
        kw = g.kwh / (g.minutes / 60)
        base = {
            "cycle_id": c.cycle_id, "start": c.start, "end": c.end, "billing_month": c.billing_month,
            "season": c.season, "tariff_versions": ";".join(sorted(g.version_id.unique())),
            "coverage_pct": c.coverage_pct,
            "baseline_kwh": g.kwh.sum(), "baseline_energy_cost": g.baseline_cost.sum(),
            "on_peak_kwh": g.kwh[g.period == "on_peak"].sum(),
        }
        for k in settings.keys():
            dest = _dest_mask(g, k)
            new_kw = g[f"{k}_kwh"] / (g.minutes / 60)
            cost = g[f"{k}_cost"].sum()
            rows.append({
                **base, "scenario": k,
                "shifted_kwh": g[f"{k}_kwh_out"].sum(),
                "destination_kwh_added": g[f"{k}_kwh_in"].sum(),
                "destination_baseline_kwh": g.kwh[dest].sum(),
                "destination_max_avg_kw_before": kw[dest].max() if dest.any() else 0.0,
                "destination_max_avg_kw_after": new_kw[dest].max() if dest.any() else 0.0,
                "scenario_kwh": g[f"{k}_kwh"].sum(),
                "scenario_energy_cost": cost,
                "modeled_savings": base["baseline_energy_cost"] - cost,
            })
    out = pd.DataFrame(rows)
    if len(out):
        out["savings_pct_of_energy_cost"] = 100 * out.modeled_savings / out.baseline_energy_cost
    return out


@dataclass
class Totals:
    label: str
    cycle_ids: list[str]
    start: str
    end: str
    by_scenario: pd.DataFrame = field(repr=False)


def _totals(summary: pd.DataFrame, ids: list[str], label: str) -> Totals:
    s = summary[summary.cycle_id.isin(ids)]
    agg = s.groupby("scenario", sort=False).agg(
        baseline_kwh=("baseline_kwh", "sum"), baseline_energy_cost=("baseline_energy_cost", "sum"),
        on_peak_kwh=("on_peak_kwh", "sum"), shifted_kwh=("shifted_kwh", "sum"),
        destination_kwh_added=("destination_kwh_added", "sum"),
        scenario_energy_cost=("scenario_energy_cost", "sum"), modeled_savings=("modeled_savings", "sum"),
    ).reset_index()
    return Totals(label, ids, s.start.min(), s.end.max(), agg)


def observed_totals(summary: pd.DataFrame) -> Totals | None:
    if summary.empty:
        return None
    return _totals(summary, list(dict.fromkeys(summary.cycle_id)), "observed period (complete cycles only, not extrapolated)")


def annual_totals(summary: pd.DataFrame, cycles: pd.DataFrame, household: Household) -> Totals | None:
    """Totals for the most recent 12 consecutive, complete billing cycles, or None."""
    status = dict(zip(cycles.cycle_id, cycles.status))
    run: list = []
    best = None
    for c in household.cycles:
        contiguous = run and (c.start - run[-1].end).days == 1
        if status.get(c.cycle_id) == "complete":
            run = run + [c] if contiguous else [c]
        else:
            run = []
        if len(run) >= 12:
            best = run[-12:]
    if best is None:
        return None
    return _totals(summary, [c.cycle_id for c in best], "12 consecutive complete billing cycles")


INTERVAL_COLUMNS = [
    "source_row", "start", "end", "minutes", "local_date", "cycle_id", "season",
    "version_id", "period", "rate_per_kwh", "kwh", "baseline_cost", "in_dest_window", "ceiling_dest",
]


def interval_export(df: pd.DataFrame, settings: ScenarioSettings) -> pd.DataFrame:
    cols = [c for c in INTERVAL_COLUMNS if c in df.columns]
    for k in settings.keys():
        cols += [f"{k}_kwh_out", f"{k}_kwh_in", f"{k}_kwh", f"{k}_cost", f"{k}_savings"]
    out = df[cols].copy()
    out["start"] = out.start.map(lambda t: t.isoformat())
    out["end"] = out.end.map(lambda t: t.isoformat())
    return out
