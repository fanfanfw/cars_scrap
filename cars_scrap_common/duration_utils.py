from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class DurationSpec:
    amount: int
    unit: str  # one of: d, w, m, y


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([dwmy])\s*$", re.IGNORECASE)


def parse_duration_spec(raw: str) -> DurationSpec:
    """
    Parses duration strings like: 1d, 7d, 1w, 2w, 1m, 3m, 1y.
    """
    if raw is None:
        raise ValueError("duration is required (example: 1w, 1m, 1y, 7d)")
    match = _DURATION_RE.match(str(raw))
    if not match:
        raise ValueError(f"Invalid duration: {raw!r}. Expected like 1w / 1m / 1y / 7d.")
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if amount < 1:
        raise ValueError(f"Invalid duration amount: {amount}. Must be >= 1.")
    if unit not in {"d", "w", "m", "y"}:
        raise ValueError(f"Invalid duration unit: {unit!r}. Use d/w/m/y.")
    return DurationSpec(amount=amount, unit=unit)


def subtract_duration(from_date: date, spec: DurationSpec) -> date:
    if spec.unit == "d":
        return from_date - timedelta(days=spec.amount)
    if spec.unit == "w":
        return from_date - timedelta(days=spec.amount * 7)
    if spec.unit == "m":
        months_to_subtract = spec.amount
        month_index = (from_date.year * 12 + (from_date.month - 1)) - months_to_subtract
        year = month_index // 12
        month = (month_index % 12) + 1
        last_day = calendar.monthrange(year, month)[1]
        day = min(from_date.day, last_day)
        return date(year, month, day)
    if spec.unit == "y":
        year = from_date.year - spec.amount
        last_day = calendar.monthrange(year, from_date.month)[1]
        day = min(from_date.day, last_day)
        return date(year, from_date.month, day)
    raise ValueError(f"Unsupported duration unit: {spec.unit!r}")


def cutoff_date(*, now: date, older_than: str) -> date:
    return subtract_duration(now, parse_duration_spec(older_than))

