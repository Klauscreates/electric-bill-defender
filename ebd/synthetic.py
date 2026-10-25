"""Synthetic usage and household files FOR TESTING ONLY. They describe no real home.

    python -m ebd.synthetic OUTDIR
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml


def synthetic_usage(start: date, days: int, minutes: int = 15, seed: int = 7) -> pd.DataFrame:
    """A plausible Phoenix load shape: base load, summer afternoon AC, evening peak."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(pd.Timestamp(start), periods=days * 24 * 60 // minutes, freq=f"{minutes}min")
    hour = idx.hour + idx.minute / 60
    summer = idx.month.isin([5, 6, 7, 8, 9, 10])
    base_kw = 0.6 + 0.4 * np.exp(-((hour - 19.5) ** 2) / 4)
    ac_kw = np.where(summer, 2.8 * np.clip(np.sin((hour - 9) / 12 * np.pi), 0, None), 0.3)
    kw = (base_kw + ac_kw) * rng.uniform(0.85, 1.15, len(idx))
    return pd.DataFrame({
        "Interval Start": idx.strftime("%m/%d/%Y %H:%M"),
        "Duration (min)": minutes,
        "kWh": np.round(kw * minutes / 60, 3),
    })


def monthly_cycles(first_start: date, n: int, read_day: int = 14) -> list[dict]:
    """Back-to-back cycles; each ends the day before the next read day."""
    cycles, s = [], first_start
    for _ in range(n):
        nxt = (s.replace(day=1) + timedelta(days=32)).replace(day=read_day)
        end = nxt - timedelta(days=1)
        cycles.append({"id": f"{nxt:%Y-%m}", "start": s, "end": end, "billing_month": f"{nxt:%Y-%m}"})
        s = nxt
    return cycles


def synthetic_household(cycles: list[dict], household_id: str = "synthetic-home") -> dict:
    return {
        "household_id": household_id,
        "synthetic": True,
        "account": {"utility": "APS", "rate_schedule": "TOU-E", "confirmed_on": "synthetic",
                    "solar": False, "battery": False, "demand_charge": False},
        "billing_cycles": cycles,
    }


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    out = Path(argv[0] if argv else "synthetic")
    out.mkdir(parents=True, exist_ok=True)
    first = date(2024, 9, 14)
    cycles = monthly_cycles(first, 13)
    days = (cycles[-1]["end"] - first).days + 1
    synthetic_usage(first, days).to_csv(out / "synthetic_usage.csv", index=False)
    (out / "synthetic_household.yaml").write_text(
        "# SYNTHETIC test household. Not a real home.\n" + yaml.safe_dump(synthetic_household(cycles), sort_keys=False),
        encoding="utf-8")
    print(f"Wrote {out / 'synthetic_usage.csv'} and {out / 'synthetic_household.yaml'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
