from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path


class ConfigError(Exception):
    """A configuration or input file is malformed."""


class UnsupportedAccount(Exception):
    """The household is outside what this version can analyze (demand, solar, battery...)."""


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def as_date(value, field: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as e:
        raise ConfigError(f"{field}: expected a YYYY-MM-DD date, got {value!r}") from e


def hhmm_to_minutes(value: str, field: str) -> int:
    try:
        h, m = str(value).split(":")
        minutes = int(h) * 60 + int(m)
    except ValueError as e:
        raise ConfigError(f"{field}: expected HH:MM, got {value!r}") from e
    if not 0 <= minutes <= 24 * 60:
        raise ConfigError(f"{field}: {value!r} is outside 00:00-24:00")
    return minutes
