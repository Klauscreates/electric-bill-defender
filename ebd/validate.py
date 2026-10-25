"""Data-quality validation. Problems are reported, never repaired: affected rows are
excluded and any billing cycle that is not fully covered by valid data is marked
incomplete and left out of savings figures."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ebd.household import Household
from ebd.tariff import Tariff, billing_month_date, classify

DEFAULT_MAX_KW = 50.0
EPS_MIN = 1e-6


@dataclass
class ValidationResult:
    rows: pd.DataFrame     # normalized rows + cycle_id, season, version_id, valid
    issues: pd.DataFrame   # severity, code, cycle_id, source_row, start, end, message
    cycles: pd.DataFrame   # one row per billing cycle with coverage and status

    @property
    def error_count(self) -> int:
        return int((self.issues.severity == "error").sum()) if len(self.issues) else 0

    @property
    def complete_cycle_ids(self) -> list[str]:
        return self.cycles.loc[self.cycles.status == "complete", "cycle_id"].tolist()


ISSUE_COLUMNS = ["severity", "code", "cycle_id", "source_row", "start", "end", "message"]


def validate(rows: pd.DataFrame, household: Household, tariff: Tariff, max_kw: float = DEFAULT_MAX_KW) -> ValidationResult:
    df = rows.copy().reset_index(drop=True)
    issues: list[dict] = []
    bad = pd.Series(False, index=df.index)

    def add(sev, code, msg, idx=None, cycle_id=None, start=None, end=None):
        r = df.loc[idx] if idx is not None else None
        issues.append({
            "severity": sev, "code": code,
            "cycle_id": cycle_id if cycle_id is not None else (r["cycle_id"] if r is not None else None),
            "source_row": int(r["source_row"]) if r is not None else None,
            "start": start if start is not None else (r["start"] if r is not None else None),
            "end": end if end is not None else (r["end"] if r is not None else None),
            "message": msg,
        })

    # --- billing cycle, season and tariff version for each row -------------------
    df["cycle_id"] = None
    df["season"] = None
    df["version_id"] = None
    timed = df.start.notna() & df.minutes.notna()
    for c in household.cycles:
        m = timed & (df.start >= c.start_ts) & (df.start < c.end_ts)
        df.loc[m, "cycle_id"] = c.cycle_id
        df.loc[m, "season"] = c.season
    in_cycle = df.cycle_id.notna()
    if tariff.version_basis == "billing_month":
        for c in household.cycles:
            v = tariff.version_for(billing_month_date(c.billing_month))
            if v:
                df.loc[df.cycle_id == c.cycle_id, "version_id"] = v.version_id
    else:
        dates = df.start.dt.date
        for d in dates[in_cycle].unique():
            v = tariff.version_for(d)
            if v:
                df.loc[in_cycle & (dates == d), "version_id"] = v.version_id

    # --- row-level problems ------------------------------------------------------
    for idx in df.index[df.import_error != ""]:
        code, _, msg = df.at[idx, "import_error"].partition(": ")
        add("error", code, f"{msg} (raw timestamp {df.at[idx, 'raw_timestamp']!r}, raw kWh {df.at[idx, 'raw_kwh']!r})", idx)
        bad[idx] = True

    neg = df.kwh < 0
    for idx in df.index[neg]:
        add("error", "NEGATIVE_KWH", f"negative consumption {df.at[idx, 'kwh']} kWh; grid export suggests solar, which is unsupported", idx)
    bad |= neg

    kw = df.kwh / (df.minutes / 60)
    high = kw > max_kw
    for idx in df.index[high]:
        add("error", "IMPLAUSIBLE_KWH", f"{df.at[idx, 'kwh']} kWh in {df.at[idx, 'minutes']:g} min is an average of {kw[idx]:.1f} kW (> {max_kw:g} kW limit)", idx)
    bad |= high

    coarse = df.minutes > 60
    for idx in df.index[coarse]:
        add("error", "INTERVAL_TOO_COARSE", f"{df.at[idx, 'minutes']:g}-minute interval cannot be split across TOU periods; hourly or finer data is required", idx)
    bad |= coarse

    # duplicates and overlaps
    dup = timed & df.start.duplicated(keep=False) & df.start.notna()
    for idx in df.index[dup]:
        n = int((df.start == df.at[idx, "start"]).sum())
        add("error", "DUPLICATE_TIMESTAMP", f"{n} rows share this interval start; none is used", idx)
    bad |= dup

    overlap = pd.Series(False, index=df.index)
    max_end, max_idx = None, None
    for idx in df[timed & ~dup].sort_values("start", kind="stable").index:
        s, e = df.at[idx, "start"], df.at[idx, "end"]
        if max_end is not None and s < max_end:
            overlap[idx] = overlap[max_idx] = True
        if max_end is None or e > max_end:
            max_end, max_idx = e, idx
    for idx in df.index[overlap]:
        add("error", "OVERLAPPING_INTERVAL", "interval overlaps another interval; neither is used", idx)
    bad |= overlap

    # intervals that straddle a TOU-period, day, season or tariff-version boundary
    check = timed & in_cycle & df.version_id.notna() & ~coarse
    if check.any():
        sub = df[check]
        next_hour = sub.start.dt.floor("h") + pd.Timedelta(hours=1)
        cand = next_hour < sub.end
        if cand.any():
            s = sub[cand]
            nh = next_hour[cand]
            p0 = classify(tariff, s.start, s.season, s.version_id)
            p1 = classify(tariff, nh, s.season, s.version_id)
            crosses = (p0 != p1) | (s.start.dt.date != nh.dt.date)
            for idx in crosses[crosses].index:
                add("error", "CROSSES_TARIFF_BOUNDARY",
                    f"interval {df.at[idx, 'start'].strftime('%H:%M')}-{df.at[idx, 'end'].strftime('%H:%M')} spans a TOU period or day boundary at {nh[idx].strftime('%Y-%m-%d %H:%M')}; not prorated",
                    idx)
                bad[idx] = True

    # --- cycle-level problems ----------------------------------------------------
    outside = timed & ~in_cycle
    if outside.any():
        add("warning", "OUTSIDE_BILLING_CYCLES",
            f"{int(outside.sum())} rows fall outside every recorded billing cycle and are not analyzed",
            start=df.start[outside].min(), end=df.end[outside].max())

    unversioned = in_cycle & df.version_id.isna()
    for cid, g in df[unversioned].groupby("cycle_id"):
        add("error", "NO_TARIFF_VERSION",
            f"{len(g)} rows predate or follow every configured tariff version (earliest {g.start.min().date()}); add the applicable revision to the tariff config",
            cycle_id=cid, start=g.start.min(), end=g.end.max())
    bad |= unversioned

    df["valid"] = ~bad & in_cycle & df.kwh.notna()

    cycle_rows = []
    for c in household.cycles:
        expected = (c.end_ts - c.start_ts).total_seconds() / 60
        span = df[timed & (df.end > c.start_ts) & (df.start < c.end_ts)].sort_values("start")
        cursor = c.start_ts
        for s, e in zip(span.start, span.end):
            if s > cursor:
                add("error", "MISSING_DATA", f"no data for {(s - cursor).total_seconds() / 3600:.2f} h", cycle_id=c.cycle_id, start=cursor, end=s)
            cursor = max(cursor, e)
        if cursor < c.end_ts:
            add("error", "MISSING_DATA", f"no data for {(c.end_ts - cursor).total_seconds() / 3600:.2f} h", cycle_id=c.cycle_id, start=cursor, end=c.end_ts)

        valid = df[df.valid & (df.cycle_id == c.cycle_id)]
        valid_min = float(valid.minutes.sum())
        n_err = sum(1 for i in issues if i["severity"] == "error" and i["cycle_id"] == c.cycle_id)
        complete = n_err == 0 and abs(valid_min - expected) < EPS_MIN
        reason = "" if complete else (f"{n_err} data errors" if n_err else "incomplete coverage")
        cycle_rows.append({
            "cycle_id": c.cycle_id, "start": c.start.isoformat(), "end": c.end.isoformat(), "days": c.days,
            "billing_month": c.billing_month, "season": c.season,
            "expected_hours": expected / 60, "valid_hours": valid_min / 60,
            "coverage_pct": 100 * valid_min / expected if expected else 0.0,
            "errors": n_err, "status": "complete" if complete else "incomplete", "reason": reason,
        })

    issue_df = pd.DataFrame(issues, columns=ISSUE_COLUMNS)
    if len(issue_df):
        issue_df = issue_df.sort_values(["severity", "code", "start"], kind="stable", key=lambda s: s.astype(str)).reset_index(drop=True)
    return ValidationResult(rows=df, issues=issue_df, cycles=pd.DataFrame(cycle_rows))


def summary_text(result: ValidationResult, max_examples: int = 3) -> str:
    lines = []
    if len(result.issues):
        for (sev, code), g in result.issues.groupby(["severity", "code"], sort=True):
            lines.append(f"  {sev.upper():7} {code:26} x{len(g)}")
            for _, r in g.head(max_examples).iterrows():
                where = f"row {r.source_row}" if pd.notna(r.source_row) else f"cycle {r.cycle_id}"
                when = r.start.isoformat() if isinstance(r.start, pd.Timestamp) else ""
                lines.append(f"          - {where} {when}: {r.message}")
    else:
        lines.append("  no issues")
    lines.append("")
    lines.append("Billing cycles:")
    cols = ["cycle_id", "start", "end", "billing_month", "season", "coverage_pct", "errors", "status"]
    lines.append(result.cycles[cols].to_string(index=False, float_format=lambda x: f"{x:.2f}"))
    return "\n".join(lines)


def issues_for_csv(issues: pd.DataFrame) -> pd.DataFrame:
    out = issues.copy()
    for col in ("start", "end"):
        out[col] = out[col].map(lambda t: t.isoformat() if isinstance(t, pd.Timestamp) else "")
    out["source_row"] = out["source_row"].map(lambda v: "" if v is None or (isinstance(v, float) and np.isnan(v)) else int(v))
    return out
