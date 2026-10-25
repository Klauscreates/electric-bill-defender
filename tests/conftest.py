"""Synthetic fixtures. They exercise the code; they say nothing about a real household."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import yaml

from ebd.household import load_household
from ebd.importer import Mapping, normalize, read_raw, write_import, load_normalized
from ebd.tariff import load_tariff

ACCOUNT = {"utility": "APS", "rate_schedule": "TOU-E", "confirmed_on": "2026-10-24",
           "solar": False, "battery": False, "demand_charge": False}


def usage(start: str, days: int, minutes: int = 60, kwh: float | None = 1.0, fn=None) -> pd.DataFrame:
    """Raw CSV-shaped usage: ISO local timestamp, duration, kWh. `fn(ts)` overrides kWh."""
    idx = pd.date_range(start, periods=days * 24 * 60 // minutes, freq=f"{minutes}min")
    vals = [fn(t) for t in idx] if fn else [kwh] * len(idx)
    return pd.DataFrame({"ts": idx.strftime("%Y-%m-%dT%H:%M:%S"), "min": minutes, "kwh": vals})


def cycle(start: str, end: str, billing_month: str, **extra) -> dict:
    return {"id": extra.pop("id", billing_month), "start": start, "end": end, "billing_month": billing_month, **extra}


@pytest.fixture
def tariff():
    return load_tariff()


@pytest.fixture
def case(tmp_path):
    """Build a workspace + household from raw rows. Returns (workspace, household_path)."""
    def build(raw: pd.DataFrame, cycles: list[dict], account: dict | None = None, name: str = "case"):
        d = tmp_path / name
        d.mkdir()
        raw.to_csv(d / "usage.csv", index=False)
        m = Mapping(kwh_col="kwh", timestamp_col="ts", interval_col="min", timestamp_marks="start")
        write_import(d / "ws", d / "usage.csv", normalize(read_raw(d / "usage.csv", m), m), m)
        hh = {"household_id": name, "synthetic": True, "account": {**ACCOUNT, **(account or {})}, "billing_cycles": cycles}
        (d / "household.yaml").write_text(yaml.safe_dump(hh, sort_keys=False), encoding="utf-8")
        return d / "ws", d / "household.yaml"
    return build


@pytest.fixture
def loaded(case, tariff):
    """Like `case` but returns (rows, household) ready for validate()."""
    def build(raw, cycles, **kw):
        ws, hp = case(raw, cycles, **kw)
        rows, _ = load_normalized(ws)
        return rows, load_household(hp, tariff)
    return build
