import json
from datetime import date

import pandas as pd
import pytest
import yaml

from ebd.analyze import OUTPUTS, run_analysis
from ebd.household import load_household
from ebd.importer import load_normalized
from ebd.reconcile import reconcile
from ebd.scenarios import ScenarioSettings, annotate, cycle_summary, run_scenarios
from ebd.synthetic import monthly_cycles, synthetic_usage
from ebd.tariff import DEFAULT_TARIFF, load_tariff
from ebd.validate import validate

from conftest import cycle, usage
from test_tariff import two_version_tariff

S_ON, S_OFF = 0.34396, 0.12345
W_ON, W_OFF, W_SOP = 0.32543, 0.12351, 0.03495


def priced(loaded, tariff, raw, cycles, settings=None):
    rows, hh = loaded(raw, cycles)
    val = validate(rows, hh, tariff)
    assert val.error_count == 0, val.issues
    df = run_scenarios(annotate(val.rows, tariff), settings or ScenarioSettings())
    return df, cycle_summary(df, val.cycles, settings or ScenarioSettings())


def by(summary, key):
    return summary.set_index("scenario").loc[key]


def test_hand_calculated_summer_weekday(loaded, tariff):
    # Tuesday 2025-07-01, flat 1 kWh every hour, billed in August (summer).
    df, s = priced(loaded, tariff, usage("2025-07-01", 1), [cycle("2025-07-01", "2025-07-01", "2025-08")])
    base = 3 * S_ON + 21 * S_OFF
    assert by(s, "shift_10").baseline_energy_cost == pytest.approx(base)
    assert by(s, "shift_10").on_peak_kwh == pytest.approx(3)
    for key, share in (("shift_10", 0.10), ("shift_25", 0.25), ("shift_50", 0.50)):
        r = by(s, key)
        assert r.shifted_kwh == pytest.approx(3 * share)
        assert r.destination_kwh_added == pytest.approx(3 * share)
        assert r.modeled_savings == pytest.approx(3 * share * (S_ON - S_OFF))
    # 50%: 1.5 kWh spread over 19:00-23:00 -> +0.375 kWh in each of 4 hours
    added = df.set_index(df.start.dt.hour)["shift_50_kwh_in"]
    assert added.loc[19:22].tolist() == pytest.approx([0.375] * 4)
    assert added.drop(index=range(19, 23)).sum() == 0
    assert by(s, "shift_50").destination_max_avg_kw_after == pytest.approx(1.375)
    assert by(s, "ceiling").modeled_savings == pytest.approx(3 * (S_ON - S_OFF))


def test_hand_calculated_winter_weekday_and_ceiling(loaded, tariff):
    # Wednesday 2025-01-15, billed January (winter).
    df, s = priced(loaded, tariff, usage("2025-01-15", 1), [cycle("2025-01-15", "2025-01-15", "2025-01")])
    assert by(s, "shift_25").baseline_energy_cost == pytest.approx(3 * W_ON + 5 * W_SOP + 16 * W_OFF)
    assert by(s, "shift_25").modeled_savings == pytest.approx(0.75 * (W_ON - W_OFF))
    c = by(s, "ceiling")
    assert c.modeled_savings == pytest.approx(3 * (W_ON - W_SOP))
    ceil_in = df.set_index(df.start.dt.hour)["ceiling_kwh_in"]
    assert ceil_in.loc[10:14].tolist() == pytest.approx([0.6] * 5)   # super off-peak only
    assert ceil_in.drop(index=range(10, 15)).sum() == 0


@pytest.mark.parametrize("day,bm", [("2025-07-05", "2025-08"), ("2025-07-04", "2025-08"), ("2025-11-27", "2025-11")])
def test_weekend_and_holiday_shift_nothing(loaded, tariff, day, bm):
    _, s = priced(loaded, tariff, usage(day, 1), [cycle(day, day, bm)])
    assert (s.shifted_kwh == 0).all() and (s.modeled_savings == 0).all()


def test_zero_shift_gives_zero_savings(loaded, tariff):
    settings = ScenarioSettings(shares=(0.0,))
    raw = usage("2025-07-01", 31, minutes=15, fn=lambda t: 0.2 + 0.1 * (t.hour % 5))
    df, s = priced(loaded, tariff, raw, [cycle("2025-07-01", "2025-07-31", "2025-08")], settings)
    assert (df.shift_0_savings == 0).all()
    assert by(s, "shift_0").modeled_savings == 0


def test_daily_energy_conserved(loaded, tariff):
    raw = synthetic_usage(date(2025, 1, 1), 60, minutes=15)
    raw = raw.rename(columns={"Interval Start": "ts", "Duration (min)": "min", "kWh": "kwh"})
    raw["ts"] = pd.to_datetime(raw.ts, format="%m/%d/%Y %H:%M").dt.strftime("%Y-%m-%dT%H:%M:%S")
    df, _ = priced(loaded, tariff, raw, [cycle("2025-01-01", "2025-01-31", "2025-01"), cycle("2025-02-01", "2025-03-01", "2025-02")])
    daily = df.groupby("local_date")
    for k in ScenarioSettings().keys():
        assert (daily[f"{k}_kwh"].sum() - daily.kwh.sum()).abs().max() < 1e-9
        assert (df[f"{k}_kwh"] >= -1e-12).all()


def test_negative_results_preserved(loaded, tmp_path):
    raw = yaml.safe_load(DEFAULT_TARIFF.read_text(encoding="utf-8"))
    raw["versions"][0]["energy_rates"]["summer"] = {"on_peak": 0.10, "off_peak": 0.20}  # inverted, test only
    p = tmp_path / "inverted.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    t = load_tariff(p)
    _, s = priced(loaded, t, usage("2025-07-01", 1), [cycle("2025-07-01", "2025-07-01", "2025-08")])
    assert by(s, "shift_50").modeled_savings == pytest.approx(-1.5 * 0.10)


def test_tariff_change_mid_cycle_prices_each_side(loaded, tmp_path):
    t = two_version_tariff(tmp_path)   # rev4 from 2025-07-16: summer 0.40 / 0.10
    _, s = priced(loaded, t, usage("2025-07-14", 4), [cycle("2025-07-14", "2025-07-17", "2025-08")])
    r = by(s, "shift_50")
    assert r.tariff_versions == "TEST-rev4;TOU-E-rev3"
    assert r.baseline_energy_cost == pytest.approx(2 * (3 * S_ON + 21 * S_OFF) + 2 * (3 * 0.40 + 21 * 0.10))
    assert r.modeled_savings == pytest.approx(2 * 1.5 * (S_ON - S_OFF) + 2 * 1.5 * (0.40 - 0.10))


# --- full runs -----------------------------------------------------------------

def synthetic_case(case, n_cycles=13, drop_day=None, bills=None):
    first = date(2024, 9, 14)
    cycles = monthly_cycles(first, n_cycles)
    days = (cycles[-1]["end"] - first).days + 1
    raw = synthetic_usage(first, days, minutes=60)
    raw = raw.rename(columns={"Interval Start": "ts", "Duration (min)": "min", "kWh": "kwh"})
    raw["ts"] = pd.to_datetime(raw.ts, format="%m/%d/%Y %H:%M").dt.strftime("%Y-%m-%dT%H:%M:%S")
    if drop_day:
        raw = raw[~raw.ts.str.startswith(drop_day)]
    for c in cycles:
        if bills and c["id"] in bills:
            c["bill"] = bills[c["id"]]
    return case(raw, cycles, name=f"syn{n_cycles}{drop_day or ''}{len(bills or {})}")


def test_annual_totals_need_twelve_complete_cycles(case, tariff, tmp_path):
    ws, hp = synthetic_case(case)
    r = run_analysis(ws, load_household(hp, tariff), tariff, tmp_path / "a")
    assert r["annual"] is not None and len(r["annual"].cycle_ids) == 12
    assert r["annual"].cycle_ids[0] == "2024-11"   # most recent 12

    ws, hp = synthetic_case(case, drop_day="2025-03-02")   # breaks the run into 6 + 6
    r = run_analysis(ws, load_household(hp, tariff), tariff, tmp_path / "b")
    assert r["annual"] is None
    assert len(r["observed"].cycle_ids) == 12
    assert "No annual total" in (tmp_path / "b" / "report.html").read_text(encoding="utf-8")


def test_outputs_are_reproducible_and_manifested(case, tariff, tmp_path):
    ws, hp = synthetic_case(case, n_cycles=3)
    hh = load_household(hp, tariff)
    r1 = run_analysis(ws, hh, tariff, tmp_path / "r1")
    run_analysis(ws, hh, tariff, tmp_path / "r2")
    for k, name in OUTPUTS.items():
        if k != "manifest":
            assert (tmp_path / "r1" / name).read_bytes() == (tmp_path / "r2" / name).read_bytes(), name
    m = json.loads((tmp_path / "r1" / "manifest.json").read_text(encoding="utf-8"))
    assert m["tariff"]["versions_used"][0]["version_id"] == "TOU-E-rev3"
    assert set(m["inputs"]) == {"raw_usage_file", "normalized_intervals", "household_config", "tariff_config"}
    assert all(len(v["sha256"]) == 64 for v in m["inputs"].values())
    assert m["scenario_settings"]["ceiling"]["label"].startswith("theoretical ceiling")
    assert m["synthetic"] is True


def test_cycle_summary_reproducible_from_interval_csv(case, tariff, tmp_path):
    ws, hp = synthetic_case(case, n_cycles=2)
    run_analysis(ws, load_household(hp, tariff), tariff, tmp_path / "o")
    iv = pd.read_csv(tmp_path / "o" / "interval_calculations.csv")
    sm = pd.read_csv(tmp_path / "o" / "cycle_scenario_summary.csv")
    assert (iv.kwh * iv.rate_per_kwh - iv.baseline_cost).abs().max() < 1e-12
    for k in ("shift_10", "shift_25", "shift_50", "ceiling"):
        recomputed = (iv.kwh - iv[f"{k}_kwh_out"] + iv[f"{k}_kwh_in"]) * iv.rate_per_kwh
        per_cycle = (iv.baseline_cost - recomputed).groupby(iv.cycle_id).sum()
        reported = sm[sm.scenario == k].set_index("cycle_id").modeled_savings
        assert (per_cycle - reported).abs().max() < 1e-9


def test_reconciliation(case, tariff, tmp_path, loaded):
    # Model the two first synthetic cycles, then enter "bills" equal to the model for one and off for another.
    ws, hp = synthetic_case(case, n_cycles=3)
    hh = load_household(hp, tariff)
    rows, _ = load_normalized(ws)
    val = validate(rows, hh, tariff)
    df = annotate(val.rows, tariff)
    lines = {}
    for cid, g in df.groupby("cycle_id"):
        lines[cid] = {p: {"kwh": round(float(x.kwh.sum()), 2), "dollars": round(float(x.baseline_cost.sum()), 2)} for p, x in g.groupby("period")}
    bills = {"2024-10": {"energy_line_items": lines["2024-10"], "other_charges": [{"name": "PSA", "dollars": 3.21}]},
             "2024-11": {"energy_line_items": lines["2024-11"]},
             "2024-12": {"energy_line_items": {**lines["2024-12"], "on_peak": {"kwh": lines["2024-12"]["on_peak"]["kwh"], "dollars": 1.00}}}}
    ws, hp = synthetic_case(case, n_cycles=3, bills=bills)
    hh = load_household(hp, tariff)
    r = run_analysis(ws, hh, tariff, tmp_path / "rec")
    rec = r["reconciliation"]
    res = dict(zip(rec.cycles.cycle_id, rec.cycles.result))
    assert res["2024-10"] == "matched within tolerance"       # summer (October billing)
    assert res["2024-11"] == "matched within tolerance"       # winter
    assert res["2024-12"].startswith("unexplained difference")
    assert rec.status == "pending" and "Synthetic" in rec.status_reason   # fixtures can never validate
    assert rec.excluded.charge.tolist() == ["PSA"]

    raw = yaml.safe_load(hp.read_text(encoding="utf-8"))
    raw["synthetic"] = False
    hp.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    r = run_analysis(ws, load_household(hp, tariff), tariff, tmp_path / "rec2")
    assert r["reconciliation"].status == "validated"
    assert r["manifest"]["reconciliation"]["status"] == "validated"


def test_reconciliation_pending_without_both_seasons(case, tariff, tmp_path):
    ws, hp = synthetic_case(case, n_cycles=2)
    r = run_analysis(ws, load_household(hp, tariff), tariff, tmp_path / "p")
    assert r["reconciliation"].status == "pending"
    assert "Household findings pending" in (tmp_path / "p" / "report.html").read_text(encoding="utf-8")


def test_no_complete_cycles_produces_report_without_savings(case, tariff, tmp_path):
    ws, hp = case(usage("2025-07-01", 10), [cycle("2025-07-01", "2025-07-31", "2025-08")])
    r = run_analysis(ws, load_household(hp, tariff), tariff, tmp_path / "n")
    assert r["observed"] is None and r["annual"] is None
    assert "No complete billing cycles" in (tmp_path / "n" / "report.html").read_text(encoding="utf-8")


def test_cli_exit_codes(case, tmp_path, capsys):
    from ebd.cli import main
    ws, hp = case(usage("2025-07-01", 10), [cycle("2025-07-01", "2025-07-31", "2025-08")])
    assert main(["validate", "--workspace", str(ws), "--household", str(hp)]) == 1
    h = yaml.safe_load(hp.read_text())
    h["account"]["demand_charge"] = True
    hp.write_text(yaml.safe_dump(h))
    assert main(["analyze", "--workspace", str(ws), "--household", str(hp), "--out", str(tmp_path / "x")]) == 3
    assert "Demand-charge plans are not supported" in capsys.readouterr().err
