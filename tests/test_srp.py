"""SRP E-21 (EZ-3, 3-6 p.m.). kWh values here are made up; no household data."""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pandas as pd
import pytest
import yaml

from ebd import LOCAL_TZ
from ebd.analyze import run_analysis
from ebd.billcheck import bill_check
from ebd.household import load_household
from ebd.tariff import TARIFF_DIR, bill_line_charge, billing_month_date, classify, find_tariff, load_tariff
from ebd.util import UnsupportedAccount

from conftest import ACCOUNT, cycle, usage

SRP_ACCOUNT = {**ACCOUNT, "utility": "SRP", "rate_schedule": "EZ-3 Plan (3-6pm)"}
# Summer, temporary FPPA (May-Oct 2026 billing cycles), as printed in SRP's ratebook p.29.
SUMMER_2026_ON = ["0.0137", "0.0071", "0.0357", "0.0168", "-0.0006", "0.0053", "0.0027", "0.0034", "0.1633", "0.0584"]
SUMMER_2026_OFF = ["0.0046", "0.0024", "0.0119", "0.0056", "-0.0006", "0.0018", "0.0009", "0.0034", "0.0280", "0.0412"]


@pytest.fixture
def srp():
    return load_tariff(TARIFF_DIR / "srp_ez3_e21.yaml")


def period(t, when, season, version):
    return classify(t, pd.Series([pd.Timestamp(when, tz=LOCAL_TZ)]), pd.Series([season]), pd.Series([version]))[0]


def test_versions_by_billing_month(srp):
    pick = lambda bm: srp.version_for(billing_month_date(bm))
    assert pick("2025-10") is None                                  # May 2019 plan not configured
    assert pick("2025-11").version_id == "E-21-2025-11"
    assert pick("2026-01").version_id == "E-21-2026-01-TCA"
    assert pick("2026-10").version_id == "E-21-2026-05-temp-FPPA"
    assert pick("2026-11").version_id == "E-21-2026-11"
    v = pick("2026-10")
    assert v.energy_rates["summer"] == {"on_peak": 0.3058, "off_peak": 0.0992}
    assert v.energy_rates["summer_peak"] == {"on_peak": 0.3623, "off_peak": 0.1031}
    assert v.energy_rates["winter"] == {"on_peak": 0.1294, "off_peak": 0.0970}
    assert pick("2025-11").energy_rates["summer"] == {"on_peak": 0.3102, "off_peak": 0.1036}


def test_three_seasons(srp):
    assert [srp.season_for_billing_month(m) for m in (5, 6, 7, 8, 9, 10, 11, 4)] == \
        ["summer", "summer", "summer_peak", "summer_peak", "summer", "summer", "winter", "winter"]


@pytest.mark.parametrize("hhmm,expected", [("14:59", "off_peak"), ("15:00", "on_peak"), ("17:59", "on_peak"), ("18:00", "off_peak")])
def test_on_peak_3_to_6(srp, hhmm, expected):
    assert period(srp, f"2026-09-15 {hhmm}", "summer", "E-21-2026-05-temp-FPPA") == expected


def test_srp_holidays_differ_from_aps(srp):
    v = "E-21-2026-05-temp-FPPA"
    assert period(srp, "2026-07-03 16:00", "summer_peak", v) == "off_peak"   # Jul 4 Sat -> observed Fri
    assert period(srp, "2026-06-19 16:00", "summer", v) == "on_peak"         # Juneteenth is not an SRP holiday
    assert period(srp, "2026-05-25 16:00", "summer", v) == "off_peak"        # Memorial Day
    assert period(srp, "2026-11-11 16:00", "winter", "E-21-2026-11") == "on_peak"  # Veterans Day is not


def _round_each(kwh: int, comps: list[str]) -> Decimal:
    return sum(((Decimal(kwh) * Decimal(c)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for c in comps), Decimal(0))


@pytest.mark.parametrize("kwh", [1, 7, 333, 512, 2045, 4817])
def test_per_component_rounding_matches_independent_calc(srp, kwh):
    v = srp.get_version("E-21-2026-05-temp-FPPA")
    assert bill_line_charge(srp, v, "summer", "on_peak", kwh) == pytest.approx(float(_round_each(kwh, SUMMER_2026_ON)), abs=1e-9)
    assert bill_line_charge(srp, v, "summer", "off_peak", kwh) == pytest.approx(float(_round_each(kwh, SUMMER_2026_OFF)), abs=1e-9)


def test_component_transcription_error_is_caught(tmp_path):
    raw = yaml.safe_load((TARIFF_DIR / "srp_ez3_e21.yaml").read_text(encoding="utf-8"))
    raw["versions"][2]["energy_rate_components"]["summer"]["on_peak"]["generation"] = 0.1634
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    from ebd.util import ConfigError
    with pytest.raises(ConfigError, match="components sum"):
        load_tariff(p)


def test_find_tariff_by_household_names():
    assert find_tariff("SRP", "EZ-3 Plan (3-6pm)").name == "srp_ez3_e21.yaml"
    assert find_tariff("aps", "TOU-E").name == "aps_tou_e.yaml"
    assert find_tariff("SRP", "TOU-E") is None


@pytest.mark.parametrize("sched,match", [("E-23", "not time-of-use"), ("E-15", "solar"), ("E-22", "not configured")])
def test_unsupported_srp_plans(tmp_path, srp, sched, match):
    p = tmp_path / "h.yaml"
    p.write_text(yaml.safe_dump({"account": {**SRP_ACCOUNT, "rate_schedule": sched},
                                 "billing_cycles": [cycle("2026-09-09", "2026-10-09", "2026-10")]}), encoding="utf-8")
    with pytest.raises(UnsupportedAccount, match=match):
        load_household(p, srp)


def test_bill_check_and_bill_level_estimate(tmp_path, srp):
    on, off = 400, 3000
    bill = {"energy_line_items": {
        "on_peak": {"kwh": on, "dollars": float(_round_each(on, SUMMER_2026_ON))},
        "off_peak": {"kwh": off, "dollars": float(_round_each(off, SUMMER_2026_OFF)) + 1.00}}}   # second line off by $1
    p = tmp_path / "h.yaml"
    p.write_text(yaml.safe_dump({"account": {**SRP_ACCOUNT, "service_charge_tier": "tier_2"}, "synthetic": False,
                                 "billing_cycles": [cycle("2026-09-09", "2026-10-09", "2026-10", bill=bill)]}), encoding="utf-8")
    r = bill_check(load_household(p, srp), srp)
    ok = dict(zip(r["checks"].line_item, r["checks"].within_tolerance))
    assert ok == {"On-Peak": True, "Off-Peak": False}
    est = r["estimates"].set_index("scenario").modeled_savings
    assert est["shift_50"] == pytest.approx(0.5 * on * (0.3058 - 0.0992))
    assert est["ceiling"] == pytest.approx(on * (0.3058 - 0.0992))
    assert r["status"] == "pending"


def test_full_srp_run(case, tmp_path, srp):
    # Tuesday-start week in the October 2026 billing cycle (summer), 1 kWh every hour.
    ws, hp = case(usage("2026-09-15", 7), [cycle("2026-09-15", "2026-09-21", "2026-10")], account=SRP_ACCOUNT)
    r = run_analysis(ws, load_household(hp, srp), srp, tmp_path / "srp")
    s = r["summary"].set_index("scenario")
    assert s.loc["shift_50", "on_peak_kwh"] == pytest.approx(5 * 3)                  # 5 weekdays x 3 h
    assert s.loc["shift_50", "modeled_savings"] == pytest.approx(7.5 * (0.3058 - 0.0992))
    iv = pd.read_csv(tmp_path / "srp" / "interval_calculations.csv")
    hours = pd.to_datetime(iv.start, utc=True).dt.tz_convert(LOCAL_TZ).dt.hour
    assert set(hours[iv.in_dest_window]) == {18, 19, 20, 21}
    html = (tmp_path / "srp" / "report.html").read_text(encoding="utf-8")
    assert "SRP E-21" in html and "summer_peak" in html
