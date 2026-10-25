"""Bill-only check: works from the kWh and dollars printed on bills, before any hourly data.

Two things it can honestly do with monthly totals:
1. Check the tariff config: price the bill's own on-peak/off-peak kWh and compare with the
   dollars the utility printed. A match to the cent shows the rates and rounding are right.
2. A bill-level sensitivity estimate: what moving X% of the bill's on-peak kWh to off-peak
   would have saved. It cannot say whether that load can move or where it lands hour by hour.
"""

from __future__ import annotations

import pandas as pd

from ebd.household import BillingCycle, Household
from ebd.reconcile import TOL_DOLLARS, cycle_version, overall_status
from ebd.scenarios import SHIFT_SHARES, shift_key
from ebd.tariff import PERIOD_LABELS, Tariff, TariffVersion, bill_line_charge


def _version(tariff: Tariff, c: BillingCycle) -> TariffVersion | None:
    v = cycle_version(tariff, c)
    if v is not None or tariff.version_basis == "billing_month":
        return v
    first, last = tariff.version_for(c.start), tariff.version_for(c.end)
    return first if first is not None and first is last else None


def bill_check(household: Household, tariff: Tariff, shares: tuple[float, ...] = SHIFT_SHARES,
               tol_dollars: float = TOL_DOLLARS) -> dict:
    checks, estimates, skipped = [], [], []
    for c in household.cycles:
        items = (c.bill or {}).get("energy_line_items") or {}
        if not items:
            continue
        v = _version(tariff, c)
        if v is None:
            skipped.append({"cycle_id": c.cycle_id, "reason": "no single configured price version covers this cycle"})
            continue
        for period, item in items.items():
            if period not in PERIOD_LABELS or item.get("kwh") is None:
                continue
            kwh = float(item["kwh"])
            modeled = bill_line_charge(tariff, v, c.season, period, kwh)
            billed = item.get("dollars")
            diff = None if billed is None else modeled - float(billed)
            ok = diff is not None and abs(diff) <= tol_dollars
            checks.append({"cycle_id": c.cycle_id, "billing_month": c.billing_month, "season": c.season, "version_id": v.version_id,
                           "line_item": PERIOD_LABELS[period], "bill_kwh": kwh, "rate_per_kwh": v.rate(c.season, period),
                           "modeled_dollars": modeled, "bill_dollars": billed, "dollar_difference": diff, "within_tolerance": ok})

        on_kwh = float((items.get("on_peak") or {}).get("kwh") or 0)
        on_rate = v.rate(c.season, "on_peak")
        off_rate = v.rate(c.season, "off_peak")
        cheapest = min(r for p, r in v.energy_rates[c.season].items() if p != "on_peak")
        base = {"cycle_id": c.cycle_id, "billing_month": c.billing_month, "season": c.season, "version_id": v.version_id,
                "bill_on_peak_kwh": on_kwh, "on_peak_rate": on_rate}
        for s in shares:
            estimates.append({**base, "scenario": shift_key(s), "shifted_kwh": s * on_kwh, "destination_rate": off_rate,
                              "modeled_savings": s * on_kwh * (on_rate - off_rate)})
        estimates.append({**base, "scenario": "ceiling", "shifted_kwh": on_kwh, "destination_rate": cheapest,
                          "modeled_savings": on_kwh * (on_rate - cheapest)})

    check_df = pd.DataFrame(checks)
    cyc = (check_df.groupby(["cycle_id", "season"], sort=False).within_tolerance.all().reset_index()
           .assign(result=lambda d: d.within_tolerance.map({True: "matched within tolerance", False: "unexplained difference"}))
           if len(check_df) else pd.DataFrame(columns=["cycle_id", "season", "result"]))
    status, reason = overall_status(cyc, household.synthetic)
    return {"checks": check_df, "estimates": pd.DataFrame(estimates), "skipped": pd.DataFrame(skipped),
            "status": status, "status_reason": reason}


def bill_check_text(r: dict) -> str:
    lines = ["Tariff check against printed bill charges (no hourly data needed):"]
    if len(r["checks"]):
        for x in r["checks"].itertuples(index=False):
            diff = "n/a" if x.dollar_difference is None or pd.isna(x.dollar_difference) else f"{x.dollar_difference:+.2f}"
            mark = "OK " if x.within_tolerance else "DIFF"
            lines.append(f"  [{mark}] {x.cycle_id} {x.season:11} {x.line_item:15} {x.bill_kwh:9,.0f} kWh x ${x.rate_per_kwh:.4f}"
                         f"  modeled ${x.modeled_dollars:,.2f}  bill ${x.bill_dollars if x.bill_dollars is not None else float('nan'):,.2f}  diff {diff}")
    else:
        lines.append("  no bill energy line items with kWh entered")
    for x in r["skipped"].itertuples(index=False) if len(r["skipped"]) else []:
        lines.append(f"  [SKIP] {x.cycle_id}: {x.reason}")
    lines += ["", "Bill-level sensitivity estimate (moves a share of the bill's on-peak kWh to off-peak; NOT validated by hourly data):"]
    if len(r["estimates"]):
        for cid, g in r["estimates"].groupby("cycle_id", sort=False):
            parts = [f"{x.scenario} ${x.modeled_savings:,.2f}" for x in g.itertuples(index=False)]
            lines.append(f"  {cid} ({g.season.iloc[0]}, {g.bill_on_peak_kwh.iloc[0]:,.0f} on-peak kWh): " + "  ".join(parts))
    lines += ["", f"Household validation: {r['status']} ({r['status_reason']})"]
    return "\n".join(lines)
