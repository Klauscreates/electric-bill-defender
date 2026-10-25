from datetime import date

import pandas as pd
import pytest
import yaml

from ebd import LOCAL_TZ
from ebd.holidays import offpeak_holidays
from ebd.tariff import DEFAULT_TARIFF, classify, load_tariff
from ebd.util import ConfigError


def period(tariff, when: str, season: str, version="TOU-E-rev3") -> str:
    ts = pd.Series([pd.Timestamp(when, tz=LOCAL_TZ)])
    return classify(tariff, ts, pd.Series([season]), pd.Series([version]))[0]


def test_config_matches_official_tariff_rev3(tariff):
    v = tariff.get_version("TOU-E-rev3")
    assert v.effective_start == date(2024, 3, 8)
    assert v.basic_service_charge_per_day == 0.458
    assert v.energy_rates == {
        "summer": {"on_peak": 0.34396, "off_peak": 0.12345},
        "winter": {"on_peak": 0.32543, "off_peak": 0.12351, "super_off_peak": 0.03495},
    }
    assert "aps.com" in v.source["url"]


@pytest.mark.parametrize("hhmm,expected", [
    ("15:59", "off_peak"), ("16:00", "on_peak"), ("18:59", "on_peak"), ("19:00", "off_peak"), ("00:00", "off_peak"),
])
def test_weekday_peak_boundaries_summer(tariff, hhmm, expected):
    assert period(tariff, f"2025-07-15 {hhmm}", "summer") == expected  # Tuesday


@pytest.mark.parametrize("hhmm,expected", [
    ("09:59", "off_peak"), ("10:00", "super_off_peak"), ("14:59", "super_off_peak"), ("15:00", "off_peak"),
    ("16:00", "on_peak"), ("19:00", "off_peak"),
])
def test_winter_super_off_peak_boundaries(tariff, hhmm, expected):
    assert period(tariff, f"2025-01-15 {hhmm}", "winter") == expected  # Wednesday


def test_no_super_off_peak_in_summer(tariff):
    assert period(tariff, "2025-07-15 12:00", "summer") == "off_peak"


@pytest.mark.parametrize("day", ["2025-07-12", "2025-07-13", "2025-01-18"])  # Sat, Sun, Sat
def test_weekends_are_off_peak_all_day(tariff, day):
    season = "summer" if day.startswith("2025-07") else "winter"
    for hhmm in ("11:00", "17:00"):
        assert period(tariff, f"{day} {hhmm}", season) == "off_peak"


def test_billing_season_comes_from_cycle_not_calendar(tariff):
    # April 22 2025 is a Tuesday. In a cycle billed in May it is summer: no super off-peak.
    assert period(tariff, "2025-04-22 12:00", "summer") == "off_peak"
    assert period(tariff, "2025-04-22 12:00", "winter") == "super_off_peak"
    # October date in a November-billed cycle is winter.
    assert period(tariff, "2025-10-28 11:00", "winter") == "super_off_peak"


def test_holiday_rules(tariff):
    rules = tariff.get_version("TOU-E-rev3").holiday_rules
    h = offpeak_holidays(rules, [2021, 2022, 2025, 2026, 2027, 2028, 2029])
    assert date(2025, 7, 4) in h                    # Friday
    assert date(2026, 7, 3) in h                    # Jul 4 2026 is Saturday -> Friday observed
    assert date(2027, 6, 18) in h                   # Juneteenth Saturday -> Friday
    assert date(2029, 11, 12) in h                  # Veterans Day Sunday -> Monday
    assert date(2021, 12, 31) in h                  # NYE itself (and Jan 1 2022 Saturday)
    assert date(2025, 11, 27) in h                  # Thanksgiving, 4th Thursday
    assert date(2026, 1, 19) in h                   # MLK, 3rd Monday
    assert date(2026, 5, 25) in h                   # Memorial Day, last Monday
    assert date(2025, 10, 13) in h                  # Indigenous Peoples' Day
    assert date(2027, 12, 24) in h                  # Christmas Eve (Friday) itself
    # Christmas Eve 2028 is a Sunday and is NOT shifted to Monday ("**" rule).
    assert date(2028, 12, 25) in h and date(2028, 12, 26) not in h
    # Easter-like non-holidays are not present
    assert date(2025, 4, 21) not in h


@pytest.mark.parametrize("when,season", [
    ("2025-07-04 17:00", "summer"),   # Independence Day, Friday
    ("2026-07-03 17:00", "summer"),   # observed Friday
    ("2025-11-27 17:00", "winter"),   # Thanksgiving
])
def test_observed_holidays_have_no_on_peak(tariff, when, season):
    assert period(tariff, when, season) == "off_peak"


def test_weekday_holiday_super_off_peak_follows_config(tariff, tmp_path):
    # Thanksgiving 2025 noon, winter
    assert period(tariff, "2025-11-27 12:00", "winter") == "off_peak"
    raw = yaml.safe_load(DEFAULT_TARIFF.read_text(encoding="utf-8"))
    raw["versions"][0]["holiday_super_off_peak"] = True
    p = tmp_path / "t.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert period(load_tariff(p), "2025-11-27 12:00", "winter") == "super_off_peak"


def two_version_tariff(tmp_path):
    raw = yaml.safe_load(DEFAULT_TARIFF.read_text(encoding="utf-8"))
    old = raw["versions"][0]
    new = yaml.safe_load(yaml.safe_dump(old))
    old["effective_end"] = "2025-07-16"
    new["version_id"] = "TEST-rev4"
    new["effective_start"] = "2025-07-16"
    new["energy_rates"]["summer"] = {"on_peak": 0.40, "off_peak": 0.10}
    raw["versions"].append(new)
    p = tmp_path / "two.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_tariff(p)


def test_tariff_effective_date_change(tmp_path):
    t = two_version_tariff(tmp_path)
    assert t.version_for(date(2025, 7, 15)).version_id == "TOU-E-rev3"
    assert t.version_for(date(2025, 7, 16)).version_id == "TEST-rev4"
    assert t.version_for(date(2024, 3, 7)) is None


def test_overlapping_versions_rejected(tmp_path):
    raw = yaml.safe_load(DEFAULT_TARIFF.read_text(encoding="utf-8"))
    dup = yaml.safe_load(yaml.safe_dump(raw["versions"][0]))
    dup["version_id"] = "x"
    dup["effective_start"] = "2025-01-01"
    raw["versions"].append(dup)
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ConfigError, match="overlap"):
        load_tariff(p)


def test_version_without_source_rejected(tmp_path):
    raw = yaml.safe_load(DEFAULT_TARIFF.read_text(encoding="utf-8"))
    del raw["versions"][0]["source"]["url"]
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ConfigError, match="source.url"):
        load_tariff(p)
