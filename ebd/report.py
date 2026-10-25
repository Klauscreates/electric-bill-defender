"""Standalone HTML report (no external assets, no scripts)."""

from __future__ import annotations

from html import escape

import pandas as pd

from ebd.scenarios import CEILING, Totals

CSS = """
:root { --bg:#fbfaf7; --fg:#1d232a; --muted:#5d6670; --line:#dcd8cf; --card:#ffffff;
        --warn-bg:#fff4dc; --warn:#8a5a00; --bad-bg:#fde8e6; --bad:#9b2c20; --ok-bg:#e6f4ea; --ok:#1e6b34; --accent:#2d5b8a; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#14171a; --fg:#e6e8ea; --muted:#9aa3ad; --line:#2d333a; --card:#1b1f24;
          --warn-bg:#3a2e12; --warn:#f0c66b; --bad-bg:#3d1f1b; --bad:#f19a8f; --ok-bg:#16301f; --ok:#8fd6a5; --accent:#8db8e6; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:1100px; margin:0 auto; padding:24px 16px 64px; }
h1 { font-size:1.6rem; margin:0 0 4px; } h2 { font-size:1.2rem; margin:36px 0 8px; border-bottom:1px solid var(--line); padding-bottom:4px; }
h3 { font-size:1rem; margin:20px 0 6px; }
p, li { max-width:75ch; } .muted { color:var(--muted); }
.banner { padding:12px 14px; border-radius:8px; margin:12px 0; border:1px solid var(--line); }
.warn { background:var(--warn-bg); color:var(--warn); } .bad { background:var(--bad-bg); color:var(--bad); } .ok { background:var(--ok-bg); color:var(--ok); }
.scroll { overflow-x:auto; border:1px solid var(--line); border-radius:8px; background:var(--card); margin:8px 0; }
table { border-collapse:collapse; width:100%; font-size:13px; font-variant-numeric:tabular-nums; }
th, td { padding:5px 9px; border-bottom:1px solid var(--line); text-align:right; white-space:nowrap; }
th { background:var(--card); position:sticky; top:0; font-weight:600; }
td.t, th.t { text-align:left; } tr:last-child td { border-bottom:none; }
code { font-size:12.5px; } .tag { display:inline-block; font-size:12px; padding:1px 7px; border-radius:10px; border:1px solid currentColor; }
"""

MONEY = {"baseline_energy_cost", "scenario_energy_cost", "modeled_savings", "modeled_dollars", "bill_dollars",
         "dollar_difference", "modeled_fixed_charge", "rate_per_kwh"}


def _fmt(col: str, v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        if col == "rate_per_kwh":
            return f"${v:.5f}"
        if col in MONEY:
            return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"
        if col.endswith("_pct") or col.startswith("savings_pct"):
            return f"{v:.1f}%"
        return f"{v:,.2f}" if isinstance(v, float) else f"{v:,}"
    return escape(str(v))


def table(df: pd.DataFrame, headers: dict[str, str] | None = None) -> str:
    if df is None or df.empty:
        return '<p class="muted">None.</p>'
    headers = headers or {}
    cols = list(df.columns)
    num = {c for c in cols if pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c])}
    head = "".join(f'<th class="{"" if c in num else "t"}">{escape(headers.get(c, c))}</th>' for c in cols)
    body = "".join(
        "<tr>" + "".join(f'<td class="{"" if c in num else "t"}">{_fmt(c, r[c])}</td>' for c in cols) + "</tr>"
        for _, r in df.iterrows()
    )
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


SCENARIO_LABEL = {"shift_10": "Shift 10%", "shift_25": "Shift 25%", "shift_50": "Shift 50%", CEILING: "Theoretical ceiling"}


def _totals_block(t: Totals | None, title: str, keys: list[str]) -> str:
    if t is None:
        return ""
    d = t.by_scenario.copy()
    d = d[d.scenario.isin(keys)]
    d["scenario"] = d.scenario.map(lambda k: SCENARIO_LABEL.get(k, k))
    return (f"<h3>{escape(title)}</h3><p class='muted'>{escape(t.label)}: {len(t.cycle_ids)} cycles, "
            f"{escape(t.start)} to {escape(t.end)}.</p>" + table(d))


def render(ctx: dict) -> str:
    hh, tariff, val, rec, settings = ctx["household"], ctx["tariff"], ctx["validation"], ctx["reconciliation"], ctx["settings"]
    summary: pd.DataFrame = ctx["summary"]
    shift_keys = [k for k in settings.keys() if k != CEILING]
    parts = [f"<h1>Electric-Bill Defender: {escape(tariff.utility)} {escape(tariff.schedule)} savings validation</h1>",
             f"<p class='muted'>Household <b>{escape(hh.household_id)}</b> · rate schedule {escape(hh.rate_schedule)} "
             f"(confirmed {escape(hh.confirmed_on or 'date not recorded')}) · tool v{escape(ctx['tool_version'])} · "
             f"run manifest <code>manifest.json</code></p>"]

    if hh.synthetic:
        parts.append("<div class='banner bad'><b>SYNTHETIC DATA.</b> This household is a test fixture. "
                     "Nothing in this report describes a real home.</div>")
    if rec.status == "validated":
        parts.append(f"<div class='banner ok'><b>Bill reconciliation: validated.</b> {escape(rec.status_reason)}.</div>")
    else:
        parts.append(f"<div class='banner warn'><b>Household findings pending.</b> {escape(rec.status_reason)}. "
                     "Figures below are model outputs, not validated estimates.</div>")

    # --- coverage
    n_complete = int((val.cycles.status == "complete").sum())
    parts.append("<h2>1. Data coverage and quality</h2>")
    parts.append(f"<p>{n_complete} of {len(val.cycles)} recorded billing cycles are complete and analyzed. "
                 "Cycles with any missing time or data error are excluded from savings figures; nothing is filled in.</p>")
    parts.append(table(val.cycles, {"coverage_pct": "coverage %", "expected_hours": "expected h", "valid_hours": "valid h"}))
    if len(val.issues):
        counts = val.issues.groupby(["severity", "code"]).size().reset_index(name="count")
        parts.append("<h3>Validation issues</h3>" + table(counts) +
                     "<p class='muted'>Every issue is listed in <code>validation_issues.csv</code>.</p>")
    else:
        parts.append("<p class='ok banner'>No validation issues.</p>")

    # --- tariff
    parts.append("<h2>2. Tariff applied</h2>")
    for vid in sorted(set(ctx["versions_used"])):
        v = tariff.get_version(vid)
        rates = pd.DataFrame([{"season": s, "period": p, "rate_per_kwh": r} for s, pr in v.energy_rates.items() for p, r in pr.items()])
        parts.append(f"<h3>{escape(v.version_id)}</h3><p>{escape(v.source.get('document', ''))}, {escape(v.source.get('decision', ''))}. "
                     f"Effective {v.effective_start}{' to ' + str(v.effective_end) if v.effective_end else ''}. "
                     f"Source: <a href='{escape(v.source['url'])}'>APS tariff PDF</a>, verified {escape(str(v.source['verified_on']))}.</p>")
        parts.append(table(rates))
        windows = [f"On-Peak {v.on_peak.text()}"]
        if v.super_off_peak:
            windows.append(f"Super Off-Peak {v.super_off_peak.text()}")
        holidays = ", ".join(h["name"] for h in v.holiday_rules)
        parts.append(f"<p class='muted'>{escape('; '.join(windows))}; all other hours Off-Peak. Seasons by billing month: "
                     f"{escape(tariff.season_text())}. Holidays with no On-Peak hours: {escape(holidays)}.</p>")
    if tariff.notes:
        parts.append("<ul>" + "".join(f"<li class='muted'>{escape(n)}</li>" for n in tariff.notes) + "</ul>")

    # --- baseline / reconciliation
    parts.append("<h2>3. Baseline reconstruction and bill reconciliation</h2>")
    parts.append("<p>Base energy charge = Σ interval kWh × per-kWh rate for its period, season (from the billing month) and tariff version. "
                 "Compared with the energy line items entered from each bill.</p>")
    if len(rec.cycles):
        parts.append(table(rec.cycles))
        parts.append(table(rec.lines))
    else:
        parts.append("<p class='warn banner'>No bill line items have been entered. Add them to the household file to reconcile.</p>")
    parts.append("<h3>Charges excluded from this model</h3><ul>" + "".join(f"<li>{escape(x)}</li>" for x in tariff.excluded_charges) + "</ul>")
    if len(rec.excluded):
        parts.append("<p>Non-energy charges entered from bills (not modeled):</p>" + table(rec.excluded))

    # --- scenarios
    parts.append("<h2>4. Load-shift sensitivity scenarios</h2>")
    parts.append("<div class='banner warn'><b>Sensitivity scenarios, not forecasts.</b> Each moves a fixed share of every weekday's "
                 f"On-Peak kWh evenly into {escape(settings.dest_window[0])}–{escape(settings.dest_window[1])} the same day. Whole-home interval data "
                 "does not show which appliances could move, what power they draw, or whether the home stays comfortable. "
                 "Modeled savings are not attributable savings.</div>")
    if summary.empty:
        parts.append("<p class='bad banner'>No complete billing cycles: no scenario results.</p>")
    else:
        cols = ["cycle_id", "billing_month", "season", "coverage_pct", "baseline_energy_cost", "on_peak_kwh", "scenario",
                "shifted_kwh", "destination_kwh_added", "destination_max_avg_kw_before", "destination_max_avg_kw_after",
                "scenario_energy_cost", "modeled_savings", "savings_pct_of_energy_cost"]
        s = summary[summary.scenario.isin(shift_keys)][cols].copy()
        s["scenario"] = s.scenario.map(SCENARIO_LABEL)
        parts.append(table(s, {"destination_max_avg_kw_before": "dest. max kW before", "destination_max_avg_kw_after": "dest. max kW after"}))
        parts.append(_totals_block(ctx["observed"], "Observed-period totals", shift_keys))
        if ctx["annual"] is not None:
            parts.append(_totals_block(ctx["annual"], "Annual totals", shift_keys))
        else:
            parts.append("<p class='muted'>No annual total: that needs 12 consecutive complete billing cycles. Totals above cover only the observed period and are not extrapolated.</p>")

        parts.append("<h2>5. Theoretical ceiling (physically unvalidated)</h2>")
        parts.append("<div class='banner bad'><b>Physically unvalidated.</b> Moves <i>all</i> On-Peak kWh into the same day's cheapest "
                     "non-On-Peak hours (Super Off-Peak where the tariff has it; otherwise all Off-Peak hours). No household could realistically do this; "
                     "it only bounds what the tariff could reward.</div>")
        c = summary[summary.scenario == CEILING][cols].drop(columns="scenario")
        parts.append(table(c, {"destination_max_avg_kw_before": "dest. max kW before", "destination_max_avg_kw_after": "dest. max kW after"}))
        parts.append(_totals_block(ctx["observed"], "Observed-period ceiling", [CEILING]))
        if ctx["annual"] is not None:
            parts.append(_totals_block(ctx["annual"], "Annual ceiling", [CEILING]))

    # --- assumptions
    parts.append("<h2>6. Assumptions and how to reproduce</h2><ul>" + "".join(f"<li>{escape(a)}</li>" for a in ctx["assumptions"]) + "</ul>")
    parts.append("<p>Reproduce any figure from <code>interval_calculations.csv</code>: "
                 "<code>baseline_cost = kwh × rate_per_kwh</code>; for scenario <i>k</i>, "
                 "<code>k_kwh = kwh − k_kwh_out + k_kwh_in</code>, <code>k_cost = k_kwh × rate_per_kwh</code>, "
                 "<code>k_savings = baseline_cost − k_cost</code>. <code>shift_N_kwh_out = N% × kwh</code> on On-Peak rows; "
                 "each day's total is spread over rows with <code>in_dest_window = yes</code> in proportion to their minutes inside the window. "
                 "Sum by <code>cycle_id</code> to get <code>cycle_scenario_summary.csv</code>.</p>")
    parts.append("<h3>Inputs</h3>" + table(pd.DataFrame(ctx["inputs_table"])))

    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Savings Validation Report</title><style>{CSS}</style></head><body><main>{''.join(parts)}</main></body></html>")
