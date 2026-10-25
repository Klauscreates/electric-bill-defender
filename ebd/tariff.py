"""Versioned tariff configuration and time-of-use period classification."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from ebd.holidays import WEEKDAYS, offpeak_holidays
from ebd.util import ConfigError, as_date, hhmm_to_minutes, sha256_file

DEFAULT_TARIFF = Path(__file__).parent / "tariffs" / "aps_tou_e.yaml"
SEASONS = ("summer", "winter")
PERIODS = ("on_peak", "off_peak", "super_off_peak")


@dataclass(frozen=True)
class Window:
    start_min: int
    end_min: int
    weekdays_only: bool
    seasons: tuple[str, ...]


@dataclass
class TariffVersion:
    version_id: str
    effective_start: date
    effective_end: date | None  # exclusive
    source: dict
    basic_service_charge_per_day: float
    energy_rates: dict[str, dict[str, float]]
    on_peak: Window
    super_off_peak: Window | None
    holiday_rules: list[dict]
    holiday_super_off_peak: bool

    def applies_to(self, d: date) -> bool:
        return d >= self.effective_start and (self.effective_end is None or d < self.effective_end)

    def rate(self, season: str, period: str) -> float:
        try:
            return self.energy_rates[season][period]
        except KeyError:
            raise ConfigError(f"{self.version_id}: no {season} {period} rate") from None


@dataclass
class Tariff:
    utility: str
    schedule: str
    name: str
    aliases: list[str]
    summer_billing_months: set[int]
    winter_billing_months: set[int]
    excluded_charges: list[str]
    versions: list[TariffVersion]
    path: Path
    sha256: str
    _holiday_cache: dict = field(default_factory=dict, repr=False)

    def matches_schedule(self, name: str) -> bool:
        return str(name).strip().lower() in {a.lower() for a in [self.schedule, *self.aliases]}

    def season_for_billing_month(self, month: int) -> str:
        if month in self.summer_billing_months:
            return "summer"
        if month in self.winter_billing_months:
            return "winter"
        raise ConfigError(f"billing month {month} is in neither season")

    def version_for(self, d: date) -> TariffVersion | None:
        for v in self.versions:
            if v.applies_to(d):
                return v
        return None

    def get_version(self, version_id: str) -> TariffVersion:
        for v in self.versions:
            if v.version_id == version_id:
                return v
        raise KeyError(version_id)

    def holidays(self, version: TariffVersion, years) -> dict[date, str]:
        key = (version.version_id, tuple(sorted(set(years))))
        if key not in self._holiday_cache:
            self._holiday_cache[key] = offpeak_holidays(version.holiday_rules, sorted(set(years)))
        return self._holiday_cache[key]


def _window(cfg: dict, name: str) -> Window:
    w = Window(
        start_min=hhmm_to_minutes(cfg["start"], f"{name}.start"),
        end_min=hhmm_to_minutes(cfg["end"], f"{name}.end"),
        weekdays_only=bool(cfg.get("weekdays_only", True)),
        seasons=tuple(cfg["seasons"]),
    )
    if w.start_min >= w.end_min:
        raise ConfigError(f"{name}: start must be before end (windows may not wrap midnight)")
    bad = set(w.seasons) - set(SEASONS)
    if bad:
        raise ConfigError(f"{name}: unknown seasons {sorted(bad)}")
    return w


def load_tariff(path: str | Path = DEFAULT_TARIFF) -> Tariff:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read tariff config {path}: {e}") from e

    versions = []
    for i, v in enumerate(raw.get("versions") or []):
        where = f"versions[{i}]"
        try:
            rates = {s: {p: float(r) for p, r in pr.items()} for s, pr in v["energy_rates"].items()}
            for s, pr in rates.items():
                if s not in SEASONS or set(pr) - set(PERIODS):
                    raise ConfigError(f"{where}.energy_rates: unknown season/period in {s}: {sorted(pr)}")
                if any(r < 0 for r in pr.values()):
                    raise ConfigError(f"{where}.energy_rates: negative rate in {s}")
            periods = v["periods"]
            on_peak = _window(periods["on_peak"], f"{where}.periods.on_peak")
            sop = _window(periods["super_off_peak"], f"{where}.periods.super_off_peak") if periods.get("super_off_peak") else None
            for h in v.get("holidays") or []:
                if h.get("type") == "nth_weekday" and h.get("weekday") not in WEEKDAYS:
                    raise ConfigError(f"{where}.holidays: bad weekday in {h}")
            version = TariffVersion(
                version_id=str(v["version_id"]),
                effective_start=as_date(v["effective_start"], f"{where}.effective_start"),
                effective_end=as_date(v["effective_end"], f"{where}.effective_end") if v.get("effective_end") else None,
                source=dict(v.get("source") or {}),
                basic_service_charge_per_day=float(v["basic_service_charge_per_day"]),
                energy_rates=rates,
                on_peak=on_peak,
                super_off_peak=sop,
                holiday_rules=list(v.get("holidays") or []),
                holiday_super_off_peak=bool(v.get("holiday_super_off_peak", False)),
            )
        except KeyError as e:
            raise ConfigError(f"{path}: {where} is missing {e}") from e
        for season in on_peak.seasons:
            version.rate(season, "on_peak")
            version.rate(season, "off_peak")
        if sop:
            for season in sop.seasons:
                version.rate(season, "super_off_peak")
        if not version.source.get("url") or not version.source.get("verified_on"):
            raise ConfigError(f"{where}: every tariff version needs source.url and source.verified_on")
        versions.append(version)

    if not versions:
        raise ConfigError(f"{path}: no tariff versions defined")
    versions.sort(key=lambda v: v.effective_start)
    for a, b in zip(versions, versions[1:]):
        if a.effective_end is None or a.effective_end > b.effective_start:
            raise ConfigError(f"tariff versions {a.version_id} and {b.version_id} overlap; set effective_end on the earlier one")

    seasons = raw.get("seasons") or {}
    summer = set(seasons.get("summer_billing_months") or [])
    winter = set(seasons.get("winter_billing_months") or [])
    if summer & winter or summer | winter != set(range(1, 13)):
        raise ConfigError(f"{path}: summer and winter billing months must partition 1-12")

    return Tariff(
        utility=raw.get("utility", ""),
        schedule=raw["schedule"],
        name=raw.get("name", raw["schedule"]),
        aliases=list(raw.get("aliases") or []),
        summer_billing_months=summer,
        winter_billing_months=winter,
        excluded_charges=list(raw.get("excluded_charges") or []),
        versions=versions,
        path=path,
        sha256=sha256_file(path),
    )


def classify(tariff: Tariff, ts: pd.Series, season: pd.Series, version_id: pd.Series) -> pd.Series:
    """TOU period ('on_peak' / 'off_peak' / 'super_off_peak') at each local instant.

    `ts` must be tz-aware America/Phoenix; `season` and `version_id` come from the
    billing cycle and tariff version the instant belongs to.
    """
    out = pd.Series("off_peak", index=ts.index, dtype=object)
    for vid in version_id.dropna().unique():
        m = version_id == vid
        v = tariff.get_version(vid)
        t = ts[m]
        s = season[m]
        minute = t.dt.hour * 60 + t.dt.minute + t.dt.second / 60
        weekday = t.dt.day_of_week < 5
        dates = t.dt.date
        hol = tariff.holidays(v, t.dt.year.unique().tolist())
        is_hol = dates.isin(list(hol))

        w = v.on_peak
        on = (minute >= w.start_min) & (minute < w.end_min) & s.isin(w.seasons) & ~is_hol
        if w.weekdays_only:
            on &= weekday
        sop = pd.Series(False, index=t.index)
        if v.super_off_peak:
            w = v.super_off_peak
            sop = (minute >= w.start_min) & (minute < w.end_min) & s.isin(w.seasons) & ~on
            if w.weekdays_only:
                sop &= weekday
            if not v.holiday_super_off_peak:
                sop &= ~is_hol
        out[m] = np.where(on, "on_peak", np.where(sop, "super_off_peak", "off_peak"))
    return out


def rates_for(tariff: Tariff, season: pd.Series, period: pd.Series, version_id: pd.Series) -> pd.Series:
    keys = pd.DataFrame({"v": version_id, "s": season, "p": period})
    lookup = {}
    for v, s, p in keys.drop_duplicates().itertuples(index=False):
        lookup[(v, s, p)] = tariff.get_version(v).rate(s, p)
    return pd.Series([lookup[k] for k in zip(keys.v, keys.s, keys.p)], index=period.index, dtype=float)
