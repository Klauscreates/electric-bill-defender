import pandas as pd
import pytest
import yaml

from ebd.household import load_household
from ebd.importer import Mapping, load_normalized, normalize, read_raw, write_import
from ebd.util import ConfigError, UnsupportedAccount
from ebd.validate import validate

from conftest import ACCOUNT, cycle, usage

JULY = [cycle("2025-07-01", "2025-07-31", "2025-08")]


def codes(res):
    return res.issues.code.value_counts().to_dict()


# --- import --------------------------------------------------------------------

def write(tmp_path, text, name="u.csv"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_missing_column_lists_available_columns(tmp_path):
    p = write(tmp_path, "When,Usage\n2025-07-01T00:00,1\n")
    m = Mapping(kwh_col="kWh", timestamp_col="When", interval_minutes=60)
    with pytest.raises(ConfigError, match=r"columns not found.*'When', 'Usage'"):
        read_raw(p, m)


def test_mapping_requires_explicit_choices():
    with pytest.raises(ConfigError):
        Mapping(kwh_col="k", timestamp_col="t").check()                       # no interval
    with pytest.raises(ConfigError):
        Mapping(kwh_col="k", timestamp_col="t", date_col="d", time_col="x", interval_minutes=15).check()


def test_malformed_values_become_import_errors(tmp_path):
    p = write(tmp_path, "t,kwh\n07/01/2025 00:00,0.5\n2025-07-01 01:00,0.5\n07/01/2025 02:00,abc\n07/01/2025 03:00,\n07/01/2025 04:00,\"1,234\"\n")
    m = Mapping(kwh_col="kwh", timestamp_col="t", timestamp_format="%m/%d/%Y %H:%M", interval_minutes=60)
    n = normalize(read_raw(p, m), m)
    errs = n.import_error.str.split(":").str[0].tolist()
    assert errs == ["", "TIMESTAMP_UNPARSEABLE", "KWH_INVALID", "KWH_INVALID", ""]
    assert n.kwh.iloc[4] == 1234.0
    assert n.source_row.tolist() == [2, 3, 4, 5, 6]


def test_end_marked_timestamps_and_date_time_columns(tmp_path):
    p = write(tmp_path, "Date,Time,kWh,Secs\n7/1/2025,1:00 AM,0.4,3600\n7/1/2025,12:00 AM,0.4,3600\n")
    m = Mapping(kwh_col="kWh", date_col="Date", time_col="Time", timestamp_format="%m/%d/%Y %I:%M %p",
                timestamp_marks="end", interval_col="Secs", interval_unit="seconds")
    n = normalize(read_raw(p, m), m)
    assert n.start.iloc[0].isoformat() == "2025-07-01T00:00:00-07:00"
    assert n.end.iloc[0].isoformat() == "2025-07-01T01:00:00-07:00"
    assert n.start.iloc[1].isoformat() == "2025-06-30T23:00:00-07:00"
    assert n.minutes.tolist() == [60, 60]


def test_offset_timestamps_converted_to_phoenix(tmp_path):
    p = write(tmp_path, "t,k\n2025-07-01T07:00:00Z,1\n2025-07-01T01:00:00-06:00,1\n")
    m = Mapping(kwh_col="k", timestamp_col="t", interval_minutes=60)
    n = normalize(read_raw(p, m), m)
    assert [t.isoformat() for t in n.start] == ["2025-07-01T00:00:00-07:00", "2025-07-01T00:00:00-07:00"]


def test_dst_ambiguous_timestamps_flagged(tmp_path):
    # 01:30 on 2025-11-02 happens twice in America/Denver; 02:30 on 2025-03-09 never happens.
    p = write(tmp_path, "t,k\n2025-11-02 01:30,1\n2025-03-09 02:30,1\n2025-07-01 12:00,1\n")
    m = Mapping(kwh_col="k", timestamp_col="t", interval_minutes=60, source_tz="America/Denver")
    n = normalize(read_raw(p, m), m)
    assert n.import_error.str.startswith("TIMESTAMP_AMBIGUOUS").tolist() == [True, True, False]
    assert n.start.iloc[2].isoformat() == "2025-07-01T11:00:00-07:00"


def test_normalized_file_tamper_detected(case):
    ws, _ = case(usage("2025-07-01", 1), JULY)
    f = ws / "normalized_intervals.csv"
    f.write_text(f.read_text() + "\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="modified"):
        load_normalized(ws)


# --- validation ----------------------------------------------------------------

def test_clean_month_is_complete(loaded, tariff):
    rows, hh = loaded(usage("2025-07-01", 31), JULY)
    res = validate(rows, hh, tariff)
    assert res.error_count == 0
    assert res.cycles.status.tolist() == ["complete"]
    assert res.cycles.coverage_pct.iloc[0] == pytest.approx(100)


def test_missing_intervals_reported_not_filled(loaded, tariff):
    raw = usage("2025-07-01", 31)
    raw = raw.drop(index=[10, 11, 12]).reset_index(drop=True)
    rows, hh = loaded(raw, JULY)
    res = validate(rows, hh, tariff)
    gaps = res.issues[res.issues.code == "MISSING_DATA"]
    assert len(gaps) == 1 and "3.00 h" in gaps.message.iloc[0]
    assert res.cycles.status.iloc[0] == "incomplete"
    assert res.cycles.coverage_pct.iloc[0] == pytest.approx(100 * (744 - 3) / 744)


def test_insufficient_coverage_at_cycle_end(loaded, tariff):
    rows, hh = loaded(usage("2025-07-01", 20), JULY)
    res = validate(rows, hh, tariff)
    assert codes(res) == {"MISSING_DATA": 1}
    assert res.complete_cycle_ids == []


def test_duplicate_timestamps(loaded, tariff):
    raw = usage("2025-07-01", 31)
    raw = pd.concat([raw, raw.iloc[[5]]]).reset_index(drop=True)
    rows, hh = loaded(raw, JULY)
    res = validate(rows, hh, tariff)
    assert codes(res) == {"DUPLICATE_TIMESTAMP": 2}
    assert res.cycles.status.iloc[0] == "incomplete"


def test_overlapping_and_boundary_crossing_intervals(loaded, tariff):
    raw = usage("2025-07-01", 31)
    # replace 15:00 and 16:00 on Jul 1 (Tue) with 15:30-16:30, which crosses the 16:00 On-Peak start,
    # and add a 30-minute row overlapping 03:00-04:00
    raw = raw[~raw.ts.isin(["2025-07-01T15:00:00", "2025-07-01T16:00:00"])]
    extra = pd.DataFrame({"ts": ["2025-07-01T15:30:00", "2025-07-01T03:30:00"], "min": [60, 30], "kwh": [1.0, 0.5]})
    rows, hh = loaded(pd.concat([raw, extra]), JULY)
    res = validate(rows, hh, tariff)
    c = codes(res)
    assert c["CROSSES_TARIFF_BOUNDARY"] == 1
    assert c["OVERLAPPING_INTERVAL"] == 2
    assert c["MISSING_DATA"] == 2  # 15:00-15:30 and 16:30-17:00


def test_hour_crossing_within_one_period_is_fine(loaded, tariff):
    raw = usage("2025-07-01", 31, minutes=30)
    raw = raw[~raw.ts.isin(["2025-07-05T11:00:00", "2025-07-05T11:30:00", "2025-07-05T12:00:00"])]  # Saturday
    extra = pd.DataFrame({"ts": ["2025-07-05T11:00:00", "2025-07-05T11:30:00"], "min": [30, 60], "kwh": [0.5, 1.0]})
    rows, hh = loaded(pd.concat([raw, extra]), JULY)
    res = validate(rows, hh, tariff)
    assert res.error_count == 0


def test_invalid_consumption(loaded, tariff):
    raw = usage("2025-07-01", 31)
    raw.loc[3, "kwh"] = -0.4
    raw.loc[4, "kwh"] = 80.0
    raw.loc[5, "kwh"] = None
    rows, hh = loaded(raw, JULY)
    res = validate(rows, hh, tariff)
    assert codes(res) == {"NEGATIVE_KWH": 1, "IMPLAUSIBLE_KWH": 1, "KWH_INVALID": 1}
    assert "solar" in res.issues[res.issues.code == "NEGATIVE_KWH"].message.iloc[0]


def test_coarse_intervals_rejected(loaded, tariff):
    raw = pd.DataFrame({"ts": pd.date_range("2025-07-01", periods=31, freq="D").strftime("%Y-%m-%dT%H:%M:%S"),
                        "min": 1440, "kwh": 30.0})
    rows, hh = loaded(raw, JULY)
    res = validate(rows, hh, tariff)
    assert codes(res) == {"INTERVAL_TOO_COARSE": 31}


def test_outside_cycles_and_unversioned_dates(loaded, tariff):
    raw = usage("2024-02-27", 15)  # tariff rev3 starts 2024-03-08
    rows, hh = loaded(raw, [cycle("2024-03-01", "2024-03-10", "2024-03")])
    res = validate(rows, hh, tariff)
    c = codes(res)
    assert c["OUTSIDE_BILLING_CYCLES"] == 1
    assert c["NO_TARIFF_VERSION"] == 1
    assert res.cycles.status.iloc[0] == "incomplete"


# --- household eligibility -------------------------------------------------------

@pytest.mark.parametrize("override,match", [
    ({"solar": True}, "Solar"),
    ({"battery": True}, "Battery"),
    ({"demand_charge": True}, "Demand-charge"),
    ({"rate_schedule": "R-2"}, "demand charge"),
    ({"rate_schedule": "R-1"}, "non-time-of-use|fixed"),
    ({"rate_schedule": "Something Else"}, "Supported plans"),
    ({"utility": "SRP"}, "does not match"),
])
def test_unsupported_accounts_rejected(tmp_path, tariff, override, match):
    p = tmp_path / "h.yaml"
    p.write_text(yaml.safe_dump({"account": {**ACCOUNT, **override}, "billing_cycles": JULY}), encoding="utf-8")
    with pytest.raises(UnsupportedAccount, match=match):
        load_household(p, tariff)


def test_eligibility_flags_must_be_explicit(tmp_path, tariff):
    acct = {k: v for k, v in ACCOUNT.items() if k != "solar"}
    p = tmp_path / "h.yaml"
    p.write_text(yaml.safe_dump({"account": acct, "billing_cycles": JULY}), encoding="utf-8")
    with pytest.raises(ConfigError, match="solar"):
        load_household(p, tariff)


def test_bill_days_mismatch_explained(tmp_path, tariff):
    p = tmp_path / "h.yaml"
    p.write_text(yaml.safe_dump({"account": ACCOUNT, "billing_cycles": [cycle("2025-07-01", "2025-07-31", "2025-08", bill_days=30)]}),
                 encoding="utf-8")
    with pytest.raises(ConfigError, match="set end to the day before"):
        load_household(p, tariff)


def test_overlapping_cycles_rejected(tmp_path, tariff):
    p = tmp_path / "h.yaml"
    p.write_text(yaml.safe_dump({"account": ACCOUNT, "billing_cycles": [
        cycle("2025-07-01", "2025-07-31", "2025-08"), cycle("2025-07-31", "2025-08-30", "2025-09")]}), encoding="utf-8")
    with pytest.raises(ConfigError, match="overlap"):
        load_household(p, tariff)
