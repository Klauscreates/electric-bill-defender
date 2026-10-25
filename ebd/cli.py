"""Command line: `ebd import`, `ebd validate`, `ebd analyze`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ebd import LOCAL_TZ
from ebd.household import load_household
from ebd.importer import Mapping, normalize, preview_text, read_raw, write_import, load_normalized
from ebd.tariff import DEFAULT_TARIFF, load_tariff
from ebd.util import ConfigError, UnsupportedAccount
from ebd.validate import DEFAULT_MAX_KW, issues_for_csv, summary_text, validate

EXIT_OK, EXIT_ISSUES, EXIT_CONFIG, EXIT_UNSUPPORTED = 0, 1, 2, 3


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ebd", description="APS TOU-E savings validation (local only).")
    sub = p.add_subparsers(dest="command", required=True)

    im = sub.add_parser("import", help="map a usage CSV into normalized intervals (preview unless --confirm)")
    im.add_argument("csv", type=Path)
    im.add_argument("--workspace", type=Path, required=True, help="folder for normalized data")
    im.add_argument("--kwh-col", required=True)
    im.add_argument("--timestamp-col")
    im.add_argument("--date-col")
    im.add_argument("--time-col")
    im.add_argument("--timestamp-format", help="strptime format, e.g. '%%m/%%d/%%Y %%I:%%M %%p'; default ISO 8601 only")
    im.add_argument("--timestamp-marks", choices=["start", "end"], required=True, help="does each timestamp mark the interval start or end?")
    im.add_argument("--interval-col")
    im.add_argument("--interval-unit", default="minutes", choices=["minutes", "seconds", "hours"])
    im.add_argument("--interval-minutes", type=float)
    im.add_argument("--source-tz", default=LOCAL_TZ, help=f"zone of timestamps without an offset (default {LOCAL_TZ})")
    im.add_argument("--skip-rows", type=int, default=0, help="lines to skip before the header row")
    im.add_argument("--encoding", default="utf-8-sig")
    im.add_argument("--rows", type=int, default=8, help="rows to show in the preview")
    im.add_argument("--confirm", action="store_true", help="write the normalized data after reviewing the preview")

    for name, help_ in (("validate", "check data quality against the household's billing cycles"),
                        ("analyze", "price the baseline, run scenarios, reconcile bills and write the report")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--workspace", type=Path, required=True)
        sp.add_argument("--household", type=Path, required=True)
        sp.add_argument("--tariff", type=Path, default=DEFAULT_TARIFF)
        sp.add_argument("--max-kw", type=float, default=DEFAULT_MAX_KW, help="average kW above which a reading is invalid")
        if name == "analyze":
            sp.add_argument("--out", type=Path, required=True, help="output folder for the report and CSVs")
    return p


def cmd_import(a) -> int:
    m = Mapping(kwh_col=a.kwh_col, timestamp_col=a.timestamp_col, date_col=a.date_col, time_col=a.time_col,
                timestamp_format=a.timestamp_format, timestamp_marks=a.timestamp_marks, interval_col=a.interval_col,
                interval_unit=a.interval_unit, interval_minutes=a.interval_minutes, source_tz=a.source_tz,
                skip_rows=a.skip_rows, encoding=a.encoding)
    m.check()
    raw = read_raw(a.csv, m)
    norm = normalize(raw, m)
    print(preview_text(raw, norm, m, a.rows))
    if not a.confirm:
        print("\nPreview only. Check the interpretation above, then re-run with --confirm to write it.")
        return EXIT_OK
    path = write_import(a.workspace, a.csv, norm, m)
    print(f"\nWrote {path}")
    return EXIT_OK


def cmd_validate(a) -> int:
    tariff = load_tariff(a.tariff)
    hh = load_household(a.household, tariff)
    rows, _ = load_normalized(a.workspace)
    res = validate(rows, hh, tariff, max_kw=a.max_kw)
    print(summary_text(res))
    out = Path(a.workspace) / "validation_issues.csv"
    issues_for_csv(res.issues).to_csv(out, index=False, lineterminator="\n")
    print(f"\nAll issues: {out}")
    return EXIT_ISSUES if res.error_count else EXIT_OK


def cmd_analyze(a) -> int:
    from ebd.analyze import run_analysis
    tariff = load_tariff(a.tariff)
    hh = load_household(a.household, tariff)
    r = run_analysis(a.workspace, hh, tariff, a.out, max_kw=a.max_kw)
    val = r["validation"]
    print(f"Cycles complete: {len(val.complete_cycle_ids)} of {len(val.cycles)}   validation errors: {val.error_count}")
    if r["observed"] is not None:
        t = r["observed"].by_scenario
        for row in t.itertuples(index=False):
            print(f"  {row.scenario:9} baseline ${row.baseline_energy_cost:,.2f}  savings ${row.modeled_savings:,.2f}  ({r['observed'].label})")
    else:
        print("  No complete billing cycles: no savings figures.")
    print(f"Bill reconciliation: {r['reconciliation'].status} ({r['reconciliation'].status_reason})")
    print(f"Report: {r['paths']['report']}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    a = _parser().parse_args(argv)
    try:
        return {"import": cmd_import, "validate": cmd_validate, "analyze": cmd_analyze}[a.command](a)
    except UnsupportedAccount as e:
        print(f"Unsupported account:\n{e}", file=sys.stderr)
        return EXIT_UNSUPPORTED
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
