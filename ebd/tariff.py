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

TARIFF_DIR = Path(__file__).parent / "tariffs"
DEFAULT_TARIFF = TARIFF_DIR / "aps_tou_e.yaml"
PERIODS = ("on_peak", "off_peak", "super_off_peak")
PERIOD_LABELS = {"on_peak": "On-Peak", "off_peak": "Off-Peak", "super_off_peak": "Super Off-Peak"}
VERSION_BASES = ("service_date", "billing_month")


@dataclass(frozen=True)
class Window:
    start_min: int
    end_min: int
    weekdays_only: bool
    seasons: tuple[str, ...]

    def text(self) -> str:
        def hm(m):
            h, mm = divmod(m, 60)
            suffix = "a.m." if h < 12 or h == 24 else "p.m."
            h12 = h % 12 or 12
            return f"{h12}{':%02d' % mm if mm else ''} {suffix}"
        days = "weekdays" if self.weekdays_only else "every day"
        return f"{hm(self.start_min)}–{hm(self.end_min)} {days} ({', '.join(self.seasons)})"


@dataclass
class TariffVersion:
    version_id: str
    effective_start: date
    effective_end: date | None  # exclusive
    source: dict
    fixed_charges: dict
    energy_rates: dict[str, dict[str, float]]
    rate_components: dict[str, dict[str, dict[str, float]]]
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

    def fixed_charge(self, days: int, tier: str | None) -> float:
        """Fixed service charge for a cycle of `days` days, or NaN when it can't be known."""
        if "per_day" in self.fixed_charges:
            return days * float(self.fixed_charges["per_day"])
        by_tier = self.fixed_charges.get("per_month_by_tier") or {}
        return float(by_tier[tier]) if tier in by_tier else float("nan")


@dataclass
class Tariff:
    utility: str
    schedule: str
    name: str
    aliases: list[str]
    version_basis: str
    seasons: dict[str, set[int]]
    shift_destination: tuple[str, str]
    bill_rounding: str
    excluded_charges: list[str]
    notes: list[str]
    versions: list[TariffVersion]
    path: Path
    sha256: str
    _holiday_cache: dict = field(default_factory=dict, repr=False)

    def matches_schedule(self, name: str) -> bool:
        return str(name).strip().lower() in {a.lower() for a in [self.schedule, *self.aliases]}

    def season_for_billing_month(self, month: int) -> str:
        for name, months in self.seasons.items():
            if month in months:
                return name
        raise ConfigError(f"billing month {month} is in no season")

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

    def season_text(self) -> str:
        names = {i: m for i, m in enumerate(["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}
        return "; ".join(f"{s}: {', '.join(names[m] for m in sorted(ms))}" for s, ms in self.seasons.items())


def _window(cfg: dict, name: str, seasons: set[str]) -> Window:
    w = Window(
        start_min=hhmm_to_minutes(cfg["start"], f"{name}.start"),
        end_min=hhmm_to_minutes(cfg["end"], f"{name}.end"),
        weekdays_only=bool(cfg.get("weekdays_only", True)),
        seasons=tuple(cfg["seasons"]),
    )
    if w.start_min >= w.end_min:
        raise ConfigError(f"{name}: start must be before end (windows may not wrap midnight)")
    bad = set(w.seasons) - seasons
    if bad:
        raise ConfigError(f"{name}: unknown seasons {sorted(bad)}")
    return w


def _effective(value, field_name: str, basis: str) -> date:
    """Service-date versions use YYYY-MM-DD; billing-month versions use YYYY-MM (first of month)."""
    if basis == "billing_month":
        text = value.strftime("%Y-%m") if isinstance(value, date) else str(value)
        try:
            y, m = text.split("-")[:2]
            return date(int(y), int(m), 1)
        except ValueError:
            raise ConfigError(f"{field_name}: expected YYYY-MM billing month, got {value!r}") from None
    return as_date(value, field_name)


def billing_month_date(label: str) -> date:
    y, m = str(label).split("-")[:2]
    return date(int(y), int(m), 1)


def load_tariff(path: str | Path = DEFAULT_TARIFF) -> Tariff:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read tariff config {path}: {e}") from e

    basis = raw.get("version_basis", "service_date")
    if basis not in VERSION_BASES:
        raise ConfigError(f"{path}: version_basis must be one of {VERSION_BASES}")
    seasons = {str(k): set(v) for k, v in (raw.get("seasons") or {}).items()}
    months = [m for ms in seasons.values() for m in ms]
    if sorted(months) != list(range(1, 13)):
        raise ConfigError(f"{path}: seasons must assign each billing month 1-12 to exactly one season")
    bill_rounding = raw.get("bill_rounding", "none")
    if bill_rounding not in ("none", "per_component"):
        raise ConfigError(f"{path}: bill_rounding must be 'none' or 'per_component'")

    versions = []
    for i, v in enumerate(raw.get("versions") or []):
        where = f"versions[{i}]"
        try:
            rates = {s: {p: float(r) for p, r in pr.items()} for s, pr in v["energy_rates"].items()}
            for s, pr in rates.items():
                if s not in seasons or set(pr) - set(PERIODS):
                    raise ConfigError(f"{where}.energy_rates: unknown season/period in {s}: {sorted(pr)}")
                if any(r < 0 for r in pr.values()):
                    raise ConfigError(f"{where}.energy_rates: negative rate in {s}")
            comps = {s: {p: {c: float(x) for c, x in cs.items()} for p, cs in pr.items()}
                     for s, pr in (v.get("energy_rate_components") or {}).items()}
            for s, pr in comps.items():
                for p, cs in pr.items():
                    if abs(sum(cs.values()) - rates[s][p]) > 1e-9:
                        raise ConfigError(f"{where}: {s} {p} components sum to {sum(cs.values()):.4f}, total says {rates[s][p]:.4f}")
            if bill_rounding == "per_component" and not comps:
                raise ConfigError(f"{where}: bill_rounding per_component needs energy_rate_components")
            periods = v["periods"]
            on_peak = _window(periods["on_peak"], f"{where}.periods.on_peak", set(seasons))
            sop = _window(periods["super_off_peak"], f"{where}.periods.super_off_peak", set(seasons)) if periods.get("super_off_peak") else None
            for h in v.get("holidays") or []:
                if h.get("type") == "nth_weekday" and h.get("weekday") not in WEEKDAYS:
                    raise ConfigError(f"{where}.holidays: bad weekday in {h}")
            version = TariffVersion(
                version_id=str(v["version_id"]),
                effective_start=_effective(v["effective_start"], f"{where}.effective_start", basis),
                effective_end=_effective(v["effective_end"], f"{where}.effective_end", basis) if v.get("effective_end") else None,
                source=dict(v.get("source") or {}),
                fixed_charges=dict(v.get("fixed_charges") or {}),
                energy_rates=rates,
                rate_components=comps,
                on_peak=on_peak,
                super_off_peak=sop,
                holiday_rules=list(v.get("holidays") or []),
                holiday_super_off_peak=bool(v.get("holiday_super_off_peak", False)),
            )
        except KeyError as e:
            raise ConfigError(f"{path}: {where} is missing {e}") from e
        for season in seasons:
            version.rate(season, "off_peak")
        for season in on_peak.seasons:
            version.rate(season, "on_peak")
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

    dest = raw.get("shift_destination") or {}
    if not dest.get("start") or not dest.get("end"):
        raise ConfigError(f"{path}: shift_destination start/end are required")

    return Tariff(
        utility=raw.get("utility", ""),
        schedule=raw["schedule"],
        name=raw.get("name", raw["schedule"]),
        aliases=list(raw.get("aliases") or []),
        version_basis=basis,
        seasons=seasons,
        shift_destination=(str(dest["start"]), str(dest["end"])),
        bill_rounding=bill_rounding,
        excluded_charges=list(raw.get("excluded_charges") or []),
        notes=list(raw.get("notes") or []),
        versions=versions,
        path=path,
        sha256=sha256_file(path),
    )


def bundled_tariffs() -> list[Path]:
    return sorted(TARIFF_DIR.glob("*.yaml"))


def find_tariff(utility: str, schedule: str) -> Path | None:
    """The bundled tariff config for a utility + rate schedule name, if one exists."""
    for p in bundled_tariffs():
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        names = {str(a).strip().lower() for a in [raw.get("schedule", ""), *(raw.get("aliases") or [])]}
        if str(raw.get("utility", "")).lower() == str(utility).strip().lower() and str(schedule).strip().lower() in names:
            return p
    return None


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


def bill_line_charge(tariff: Tariff, version: TariffVersion, season: str, period: str, kwh: float) -> float:
    """What the utility would print for `kwh` in one period, using its rounding convention."""
    if tariff.bill_rounding == "per_component":
        comps = version.rate_components[season][period]
        # Utilities round half up to the cent on each printed component.
        return sum(float(np.floor(kwh * c * 100 + 0.5 + 1e-9) / 100) if kwh * c >= 0
                   else -float(np.floor(-kwh * c * 100 + 0.5 + 1e-9) / 100) for c in comps.values())
    return kwh * version.rate(season, period)
