"""Compare reconstructed base energy charges with the energy line items on real bills."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ebd.household import BillingCycle, Household
from ebd.tariff import PERIOD_LABELS, Tariff, TariffVersion, bill_line_charge, billing_month_date

TOL_DOLLARS = 0.05
TOL_KWH = 1.0


@dataclass
class Reconciliation:
    lines: pd.DataFrame        # one row per cycle x line item compared
    cycles: pd.DataFrame       # one row per cycle that has bill data
    excluded: pd.DataFrame     # non-energy charges listed on bills, not modeled
    status: str                # "validated" or "pending"
    status_reason: str


def cycle_version(tariff: Tariff, c: BillingCycle) -> TariffVersion | None:
    """The single price version for a billing-month tariff (None for service-date tariffs)."""
    if tariff.version_basis == "billing_month":
        return tariff.version_for(billing_month_date(c.billing_month))
    return None


def fixed_charge(tariff: Tariff, c: BillingCycle, tier: str | None) -> float:
    v = cycle_version(tariff, c)
    if v is not None:
        return v.fixed_charge(c.days, tier)
    total = 0.0
    for d in pd.date_range(c.start, c.end, freq="D"):
        v = tariff.version_for(d.date())
        if v is None:
            return float("nan")
        total += v.fixed_charge(1, tier)
    return total


def modeled_line(tariff: Tariff, sel: pd.DataFrame, season: str, period: str) -> float:
    """Modeled bill dollars for one period's intervals, per version, with the utility's rounding."""
    return sum(bill_line_charge(tariff, tariff.get_version(vid), season, period, float(g.kwh.sum()))
               for vid, g in sel.groupby("version_id"))


def overall_status(cyc: pd.DataFrame, synthetic: bool) -> tuple[str, str]:
    if synthetic:
        return "pending", "Synthetic data cannot validate a household; reconcile real bills"
    matched = set(cyc.loc[cyc.result == "matched within tolerance", "season"])
    have_warm = any(s != "winter" for s in matched)
    have_winter = "winter" in matched
    if have_warm and have_winter:
        return "validated", "at least one summer and one winter bill reconcile within tolerance"
    missing = [x for x, ok in (("summer", have_warm), ("winter", have_winter)) if not ok]
    return "pending", f"No reconciled {' or '.join(missing)} bill yet; household estimates are not validated"


def reconcile(df: pd.DataFrame, cycles: pd.DataFrame, household: Household, tariff: Tariff,
              tol_dollars: float = TOL_DOLLARS, tol_kwh: float = TOL_KWH) -> Reconciliation:
    status_by_id = dict(zip(cycles.cycle_id, cycles.status))
    lines, cyc, excluded = [], [], []

    for c in household.cycles:
        if not c.bill:
            continue
        bill = c.bill
        for item in bill.get("other_charges") or []:
            excluded.append({"cycle_id": c.cycle_id, "charge": item.get("name", ""), "bill_dollars": item.get("dollars")})
        modeled_fixed = fixed_charge(tariff, c, household.service_charge_tier)

        if status_by_id.get(c.cycle_id) != "complete":
            cyc.append({"cycle_id": c.cycle_id, "season": c.season, "billing_month": c.billing_month,
                        "result": "not reconcilable: usage data for this cycle is incomplete",
                        "modeled_fixed_charge": modeled_fixed, "notes": c.notes})
            continue

        g = df[df.cycle_id == c.cycle_id]
        mismatched = 0
        compared = 0
        for period, item in (bill.get("energy_line_items") or {}).items():
            if period not in PERIOD_LABELS:
                raise ValueError(f"cycle {c.cycle_id}: unknown energy line item {period!r}; use {list(PERIOD_LABELS)}")
            sel = g[g.period == period]
            m_kwh, m_usd = sel.kwh.sum(), modeled_line(tariff, sel, c.season, period)
            b_kwh, b_usd = item.get("kwh"), item.get("dollars")
            row = {"cycle_id": c.cycle_id, "season": c.season, "line_item": PERIOD_LABELS[period],
                   "modeled_kwh": m_kwh, "bill_kwh": b_kwh, "kwh_difference": None,
                   "modeled_dollars": m_usd, "bill_dollars": b_usd, "dollar_difference": None, "within_tolerance": True}
            if b_kwh is not None:
                row["kwh_difference"] = m_kwh - float(b_kwh)
                row["within_tolerance"] &= abs(row["kwh_difference"]) <= tol_kwh
            if b_usd is not None:
                row["dollar_difference"] = m_usd - float(b_usd)
                row["within_tolerance"] &= abs(row["dollar_difference"]) <= tol_dollars
            compared += 1
            mismatched += not row["within_tolerance"]
            lines.append(row)

        if bill.get("energy_total_dollars") is not None:
            m_usd = sum(modeled_line(tariff, g[g.period == p], c.season, p) for p in g.period.unique())
            diff = m_usd - float(bill["energy_total_dollars"])
            ok = abs(diff) <= tol_dollars
            lines.append({"cycle_id": c.cycle_id, "season": c.season, "line_item": "Energy total",
                          "modeled_kwh": g.kwh.sum(), "bill_kwh": bill.get("energy_total_kwh"),
                          "kwh_difference": (g.kwh.sum() - float(bill["energy_total_kwh"])) if bill.get("energy_total_kwh") is not None else None,
                          "modeled_dollars": m_usd, "bill_dollars": bill["energy_total_dollars"],
                          "dollar_difference": diff, "within_tolerance": ok})
            compared += 1
            mismatched += not ok

        result = ("no energy line items entered" if compared == 0
                  else "matched within tolerance" if mismatched == 0
                  else f"unexplained difference on {mismatched} of {compared} line items")
        cyc.append({"cycle_id": c.cycle_id, "season": c.season, "billing_month": c.billing_month, "result": result,
                    "modeled_fixed_charge": modeled_fixed, "notes": c.notes})

    cyc_df = pd.DataFrame(cyc, columns=["cycle_id", "season", "billing_month", "result", "modeled_fixed_charge", "notes"])
    status, reason = overall_status(cyc_df, household.synthetic)
    return Reconciliation(
        lines=pd.DataFrame(lines, columns=["cycle_id", "season", "line_item", "modeled_kwh", "bill_kwh", "kwh_difference",
                                           "modeled_dollars", "bill_dollars", "dollar_difference", "within_tolerance"]),
        cycles=cyc_df,
        excluded=pd.DataFrame(excluded, columns=["cycle_id", "charge", "bill_dollars"]),
        status=status, status_reason=reason,
    )
