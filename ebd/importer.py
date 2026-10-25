"""Import a usage CSV through an explicit column mapping into normalized Phoenix-time intervals.

No column names or timestamp formats are guessed: the caller names every column, and
anything that cannot be interpreted exactly is kept as a row with an `import_error`
so validation can report it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from ebd import LOCAL_TZ, __version__
from ebd.util import ConfigError, sha256_file

NORMALIZED = "normalized_intervals.csv"
IMPORT_MANIFEST = "import_manifest.json"
OFFSET_RE = r"(?:Z|[+-]\d{2}:?\d{2})\s*$"
UNIT_MINUTES = {"minutes": 1.0, "seconds": 1 / 60, "hours": 60.0}


@dataclass
class Mapping:
    kwh_col: str
    timestamp_col: str | None = None
    date_col: str | None = None
    time_col: str | None = None
    timestamp_format: str | None = None   # strptime format; None = ISO 8601 only
    timestamp_marks: str = "start"        # does the timestamp mark interval start or end?
    interval_col: str | None = None
    interval_unit: str = "minutes"
    interval_minutes: float | None = None
    source_tz: str = LOCAL_TZ             # zone of naive timestamps
    skip_rows: int = 0
    encoding: str = "utf-8-sig"

    def check(self) -> None:
        if bool(self.timestamp_col) == bool(self.date_col or self.time_col):
            raise ConfigError("map either --timestamp-col, or both --date-col and --time-col")
        if (self.date_col is None) != (self.time_col is None):
            raise ConfigError("--date-col and --time-col must be given together")
        if bool(self.interval_col) == (self.interval_minutes is not None):
            raise ConfigError("give exactly one of --interval-col or --interval-minutes")
        if self.interval_minutes is not None and self.interval_minutes <= 0:
            raise ConfigError("--interval-minutes must be positive")
        if self.timestamp_marks not in ("start", "end"):
            raise ConfigError("--timestamp-marks must be 'start' or 'end'")
        if self.interval_unit not in UNIT_MINUTES:
            raise ConfigError(f"--interval-unit must be one of {sorted(UNIT_MINUTES)}")


def read_raw(path: str | Path, m: Mapping) -> pd.DataFrame:
    try:
        raw = pd.read_csv(path, dtype=str, keep_default_na=False, skiprows=m.skip_rows, encoding=m.encoding)
    except (OSError, UnicodeDecodeError, pd.errors.ParserError, pd.errors.EmptyDataError) as e:
        raise ConfigError(f"cannot read {path} as CSV: {e}") from e
    raw.columns = [str(c).strip() for c in raw.columns]
    wanted = [c for c in (m.timestamp_col, m.date_col, m.time_col, m.kwh_col, m.interval_col) if c]
    missing = [c for c in wanted if c not in raw.columns]
    if missing:
        raise ConfigError(f"columns not found: {missing}. Columns in file: {list(raw.columns)}")
    return raw


def _parse_times(text: pd.Series, m: Mapping) -> tuple[pd.Series, pd.Series]:
    """Parse to tz-aware Phoenix timestamps. Returns (timestamps, error codes)."""
    text = text.str.strip()
    err = pd.Series("", index=text.index, dtype=object)
    ts = pd.Series(pd.NaT, index=text.index, dtype=f"datetime64[ns, {LOCAL_TZ}]")
    has_offset = text.str.contains(OFFSET_RE, regex=True)
    fmt = m.timestamp_format or "ISO8601"

    if has_offset.any():
        parsed = pd.to_datetime(text[has_offset], format=fmt, errors="coerce", utc=True)
        ts[has_offset] = parsed.dt.tz_convert(LOCAL_TZ)
    naive = ~has_offset
    if naive.any():
        parsed = pd.to_datetime(text[naive], format=fmt, errors="coerce")
        local = parsed.dt.tz_localize(m.source_tz, ambiguous="NaT", nonexistent="NaT")
        ambiguous = parsed.notna() & local.isna()
        err[ambiguous[ambiguous].index] = (
            f"TIMESTAMP_AMBIGUOUS: local time is ambiguous or nonexistent in {m.source_tz} (DST change)"
        )
        ts[naive] = local.dt.tz_convert(LOCAL_TZ)
    unparsed = ts.isna() & (err == "")
    err[unparsed] = f"TIMESTAMP_UNPARSEABLE: does not match format {fmt!r}"
    return ts, err


def normalize(raw: pd.DataFrame, m: Mapping) -> pd.DataFrame:
    m.check()
    header_line = m.skip_rows + 1
    if m.timestamp_col:
        ts_text = raw[m.timestamp_col]
    else:
        ts_text = raw[m.date_col].str.strip() + " " + raw[m.time_col].str.strip()
    ts, err = _parse_times(ts_text, m)

    kwh_text = raw[m.kwh_col].str.strip().str.replace(",", "", regex=False)
    kwh = pd.to_numeric(kwh_text, errors="coerce")
    bad_kwh = kwh.isna() & (err == "")
    err[bad_kwh] = "KWH_INVALID: kWh value is blank or not a number"

    if m.interval_col:
        minutes = pd.to_numeric(raw[m.interval_col].str.strip(), errors="coerce") * UNIT_MINUTES[m.interval_unit]
    else:
        minutes = pd.Series(float(m.interval_minutes), index=raw.index)
    bad_min = (minutes.isna() | (minutes <= 0)) & (err == "")
    err[bad_min] = "INTERVAL_INVALID: interval duration is blank, zero, negative or not a number"
    minutes = minutes.where(minutes > 0)

    delta = pd.to_timedelta(minutes, unit="min")
    if m.timestamp_marks == "start":
        start, end = ts, ts + delta
    else:
        start, end = ts - delta, ts

    return pd.DataFrame({
        "source_row": raw.index + header_line + 1,  # 1-based line number in the file
        "raw_timestamp": ts_text.values,
        "raw_kwh": raw[m.kwh_col].values,
        "start": start,
        "end": end,
        "minutes": minutes,
        "kwh": kwh,
        "import_error": err.values,
    })


def preview_text(raw: pd.DataFrame, norm: pd.DataFrame, m: Mapping, n: int = 8) -> str:
    lines = [f"Columns in file: {list(raw.columns)}", "", f"First {min(n, len(raw))} raw rows:"]
    lines.append(raw.head(n).to_string(index=False))
    lines += ["", "Interpreted as (America/Phoenix, interval start..end):"]
    show = norm.head(n).copy()
    show["start"] = show["start"].map(lambda t: "" if pd.isna(t) else t.isoformat())
    show["end"] = show["end"].map(lambda t: "" if pd.isna(t) else t.isoformat())
    lines.append(show[["source_row", "raw_timestamp", "start", "end", "minutes", "kwh", "import_error"]].to_string(index=False))
    ok = norm[norm.import_error == ""]
    lines += ["", f"Rows: {len(norm)}   interpretable: {len(ok)}   with import errors: {len(norm) - len(ok)}"]
    if len(ok):
        lines.append(f"Span: {ok.start.min().isoformat()} .. {ok.end.max().isoformat()}")
        lines.append(f"Interval lengths (minutes -> rows): {ok.minutes.value_counts().sort_index().to_dict()}")
        lines.append(f"Total kWh (interpretable rows): {ok.kwh.sum():.3f}")
    errs = norm.loc[norm.import_error != "", "import_error"].str.split(":").str[0].value_counts()
    if len(errs):
        lines.append(f"Import error codes: {errs.to_dict()}")
    lines.append(f"Timestamp marks interval {m.timestamp_marks.upper()}; naive times read as {m.source_tz}.")
    return "\n".join(lines)


def write_import(workspace: str | Path, source: str | Path, norm: pd.DataFrame, m: Mapping) -> Path:
    ws = Path(workspace)
    ws.mkdir(parents=True, exist_ok=True)
    out = norm.copy()
    for col in ("start", "end"):
        out[col] = out[col].map(lambda t: "" if pd.isna(t) else t.isoformat())
    out.to_csv(ws / NORMALIZED, index=False)
    ok = norm[norm.import_error == ""]
    manifest = {
        "tool_version": __version__,
        "source_file": str(Path(source).resolve()),
        "source_sha256": sha256_file(source),
        "mapping": asdict(m),
        "rows": int(len(norm)),
        "rows_with_import_errors": int(len(norm) - len(ok)),
        "normalized_file": NORMALIZED,
        "normalized_sha256": sha256_file(ws / NORMALIZED),
    }
    (ws / IMPORT_MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return ws / NORMALIZED


def load_normalized(workspace: str | Path) -> tuple[pd.DataFrame, dict]:
    ws = Path(workspace)
    if not (ws / NORMALIZED).exists():
        raise ConfigError(f"{ws / NORMALIZED} not found; run `ebd import ... --confirm` first")
    df = pd.read_csv(ws / NORMALIZED, dtype={"raw_timestamp": str, "raw_kwh": str, "import_error": str}, keep_default_na=False,
                     na_values={"minutes": [""], "kwh": [""]})
    for col in ("start", "end"):
        df[col] = pd.to_datetime(df[col].replace("", None), utc=True).dt.tz_convert(LOCAL_TZ)
    df["minutes"] = pd.to_numeric(df["minutes"], errors="coerce")
    df["kwh"] = pd.to_numeric(df["kwh"], errors="coerce")
    df["import_error"] = df["import_error"].fillna("")
    manifest = json.loads((ws / IMPORT_MANIFEST).read_text(encoding="utf-8"))
    if manifest.get("normalized_sha256") != sha256_file(ws / NORMALIZED):
        raise ConfigError(f"{ws / NORMALIZED} was modified after import; re-run the import")
    return df, manifest
