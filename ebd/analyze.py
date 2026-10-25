"""End-to-end analysis run: validate, price, run scenarios, reconcile, export."""

from __future__ import annotations

import json
import platform
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ebd import LOCAL_TZ, __version__
from ebd.household import Household
from ebd.importer import NORMALIZED, load_normalized
from ebd.reconcile import TOL_DOLLARS, TOL_KWH, reconcile
from ebd.report import render
from ebd.scenarios import ScenarioSettings, annotate, annual_totals, cycle_summary, interval_export, observed_totals, run_scenarios
from ebd.tariff import Tariff
from ebd.util import sha256_file
from ebd.validate import DEFAULT_MAX_KW, issues_for_csv, validate

OUTPUTS = {
    "report": "report.html",
    "intervals": "interval_calculations.csv",
    "summary": "cycle_scenario_summary.csv",
    "coverage": "cycle_coverage.csv",
    "issues": "validation_issues.csv",
    "reconciliation": "bill_reconciliation.csv",
    "manifest": "manifest.json",
}


def assumptions(tariff: Tariff, settings: ScenarioSettings, max_kw: float) -> list[str]:
    version_rule = (
        "Price version is chosen by each cycle's billing month, as the utility applies price changes by billing cycle."
        if tariff.version_basis == "billing_month" else
        "Tariff version is chosen per interval by its local date; a cycle that spans a rate change is priced in both versions."
    )
    return [
        f"Only {tariff.utility} {tariff.schedule} ({tariff.name}) without solar, battery or demand charges is modeled.",
        "All times are America/Phoenix (no daylight saving). Each interval is priced by its start time; intervals that straddle a TOU boundary are rejected, not prorated.",
        "Billing cycles run from the first through the last service day shown on the bill, midnight to midnight. Actual meter reads happen during the day, so a few hours at each cycle edge may be billed in the neighbouring cycle; bill reconciliation reveals this.",
        f"Season comes from each cycle's billing month ({tariff.season_text()}), as the tariff specifies, not from the calendar date of each interval.",
        version_rule,
        *tariff.notes,
        "Energy charges use the per-kWh energy rates only. Fixed service charges, riders, taxes and fees are excluded (listed in section 3).",
        f"Shift scenarios move {', '.join(f'{s:.0%}' for s in settings.shares)} of each weekday's On-Peak kWh into {settings.dest_window[0]}–{settings.dest_window[1]} the same day, spread evenly by time; daily kWh is unchanged.",
        "Weekends and tariff holidays have no On-Peak hours, so nothing is shifted on them.",
        "The ceiling moves 100% of On-Peak kWh to the same day's lowest-priced eligible hours and is physically unvalidated.",
        f"Readings above an average of {max_kw:g} kW in one interval are treated as invalid.",
        "Missing, duplicate, overlapping or invalid data is never filled or averaged; affected cycles are excluded.",
        "Annual totals appear only for 12 consecutive complete billing cycles; otherwise the observed period is reported without extrapolation.",
        f"Bill reconciliation tolerance: ±${TOL_DOLLARS:.2f} and ±{TOL_KWH:g} kWh per line item.",
    ]


def _csv(df: pd.DataFrame, path: Path) -> None:
    df.to_csv(path, index=False, lineterminator="\n")


def run_analysis(workspace: str | Path, household: Household, tariff: Tariff, out_dir: str | Path,
                 settings: ScenarioSettings | None = None, max_kw: float = DEFAULT_MAX_KW) -> dict:
    settings = settings or ScenarioSettings.for_tariff(tariff)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows, import_manifest = load_normalized(workspace)

    val = validate(rows, household, tariff, max_kw=max_kw)
    complete = val.complete_cycle_ids
    priced = annotate(val.rows[val.rows.cycle_id.isin(complete)], tariff)
    if priced.empty:
        scen = priced
        summary = pd.DataFrame()
    else:
        scen = run_scenarios(priced, settings)
        summary = cycle_summary(scen, val.cycles, settings)
    rec = reconcile(scen if not scen.empty else priced, val.cycles, household, tariff)
    observed = observed_totals(summary)
    annual = annual_totals(summary, val.cycles, household) if not summary.empty else None
    versions_used = sorted(scen.version_id.unique()) if not scen.empty else []

    paths = {k: out / v for k, v in OUTPUTS.items()}
    if not scen.empty:
        _csv(interval_export(scen, settings), paths["intervals"])
    else:
        paths["intervals"].write_text("no complete billing cycles\n", encoding="utf-8")
    _csv(summary, paths["summary"])
    _csv(val.cycles, paths["coverage"])
    _csv(issues_for_csv(val.issues), paths["issues"])
    _csv(rec.lines, paths["reconciliation"])

    ws = Path(workspace)
    inputs = {
        "raw_usage_file": {"path": import_manifest["source_file"], "sha256": import_manifest["source_sha256"]},
        "normalized_intervals": {"path": str((ws / NORMALIZED).resolve()), "sha256": sha256_file(ws / NORMALIZED)},
        "household_config": {"path": str(household.path.resolve()), "sha256": household.sha256},
        "tariff_config": {"path": str(tariff.path.resolve()), "sha256": tariff.sha256},
    }
    ctx = {
        "household": household, "tariff": tariff, "validation": val, "reconciliation": rec, "settings": settings,
        "summary": summary, "observed": observed, "annual": annual, "versions_used": versions_used,
        "tool_version": __version__, "assumptions": assumptions(tariff, settings, max_kw),
        "inputs_table": [{"input": k, "sha256": v["sha256"][:16] + "…", "path": Path(v["path"]).name} for k, v in inputs.items()],
    }
    paths["report"].write_text(render(ctx), encoding="utf-8")

    manifest = {
        "tool": "electric-bill-defender", "tool_version": __version__,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": platform.python_version(), "pandas": pd.__version__,
        "household_id": household.household_id, "synthetic": household.synthetic,
        "timezone": LOCAL_TZ,
        "inputs": inputs,
        "import_mapping": import_manifest["mapping"],
        "tariff": {
            "utility": tariff.utility,
            "schedule": tariff.schedule,
            "version_basis": tariff.version_basis,
            "versions_used": [
                {"version_id": v, "effective_start": str(tariff.get_version(v).effective_start),
                 "effective_end": str(tariff.get_version(v).effective_end) if tariff.get_version(v).effective_end else None,
                 "source": {k: str(x) for k, x in tariff.get_version(v).source.items()}}
                for v in versions_used
            ],
        },
        "scenario_settings": settings.describe(),
        "validation": {
            "errors": val.error_count,
            "warnings": int((val.issues.severity == "warning").sum()) if len(val.issues) else 0,
            "cycles_total": len(val.cycles), "cycles_complete": len(complete),
        },
        "reconciliation": {"status": rec.status, "reason": rec.status_reason},
        "annual_totals_available": annual is not None,
        "assumptions": ctx["assumptions"],
        "outputs": {k: {"file": p.name, "sha256": sha256_file(p)} for k, p in paths.items() if k != "manifest"},
    }
    paths["manifest"].write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"paths": paths, "validation": val, "summary": summary, "reconciliation": rec,
            "observed": observed, "annual": annual, "manifest": manifest}
