"""Rule-based explicit temporal normalization for V4."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

DEEP_TIME_UNITS = {
    "million": 1_000_000,
    "m": 1_000_000,
    "billion": 1_000_000_000,
    "bn": 1_000_000_000,
}


@dataclass(frozen=True)
class TemporalMatch:
    """Normalized temporal match candidate."""

    time_ref_id: str
    time_kind: str
    label: str
    precision: str
    start_date: str | None
    end_date: str | None
    year: int | None = None
    month: int | None = None
    day: int | None = None
    season: str | None = None
    era_name: str | None = None
    region_scope: str | None = None
    metadata_json: dict[str, Any] | None = None


def _iso_year_bounds(year: int) -> tuple[str, str]:
    return f"{year:04d}-01-01", f"{year:04d}-12-31"


def _signed_year(year: int) -> str:
    if year < 0:
        return f"-{abs(year):04d}"
    return f"{year:04d}"


def _historical_year_bounds(year: int) -> tuple[str, str]:
    signed = _signed_year(year)
    return f"{signed}-01-01", f"{signed}-12-31"


def _iso_month_bounds(year: int, month: int) -> tuple[str, str]:
    if month == 12:
        next_month = datetime(year + 1, 1, 1)
    else:
        next_month = datetime(year, month + 1, 1)
    start = datetime(year, month, 1).strftime("%Y-%m-%d")
    end = (next_month.replace(day=1) - datetime.resolution).strftime("%Y-%m-%d")
    return start, end


def _season_bounds(year: int, season: str) -> tuple[str, str]:
    normalized = "fall" if season == "autumn" else season
    if normalized == "spring":
        return f"{year:04d}-03-01", f"{year:04d}-05-31"
    if normalized == "summer":
        return f"{year:04d}-06-01", f"{year:04d}-08-31"
    if normalized == "fall":
        return f"{year:04d}-09-01", f"{year:04d}-11-30"
    return f"{year:04d}-12-01", f"{year + 1:04d}-02-28"


def _looks_like_coordinate_year(text: str, start: int, end: int) -> bool:
    """Reject bare years inside image-map/SVG coordinate runs."""

    window = text[max(0, start - 80) : min(len(text), end + 80)]
    lower = window.lower()
    numeric_tokens = re.findall(r"\b\d{2,4}\b", window)
    if ("poly" in lower or "coords" in lower) and len(numeric_tokens) >= 8:
        return True
    alpha_tokens = re.findall(r"\b[A-Za-z]{2,}\b", window)
    return len(numeric_tokens) >= 12 and len(numeric_tokens) > len(alpha_tokens) * 3


def normalize_temporal_mentions(text: str) -> list[TemporalMatch]:
    """Extract explicit date/month/season/year references."""

    found: dict[str, TemporalMatch] = {}

    # Deep-time expressions: "541 million years ago", "4.5 billion years ago".
    deep_time_pattern = r"\b(?:about|around|approximately|roughly|circa|c\.)?\s*(\d+(?:\.\d+)?)\s*(million|billion|m|bn)\s+years?\s+ago\b"
    for match in re.finditer(deep_time_pattern, text, flags=re.IGNORECASE):
        amount = float(match.group(1))
        unit = match.group(2).lower()
        multiplier = DEEP_TIME_UNITS[unit]
        years_ago = int(round(amount * multiplier))
        label_amount = int(amount) if amount.is_integer() else amount
        unit_label = "million" if multiplier == 1_000_000 else "billion"
        ref = f"ti:deep_time:{years_ago}ya"
        found[ref] = TemporalMatch(
            time_ref_id=ref,
            time_kind="deep_time",
            label=f"{label_amount} {unit_label} years ago",
            precision="deep_time",
            start_date=None,
            end_date=None,
            year=None,
            metadata_json={
                "source_pattern": "deep_time_years_ago",
                "years_ago": years_ago,
                "amount": amount,
                "unit": unit_label,
            },
        )

    # Explicit era years: "476 CE", "300 BC", "44 BCE", "27 AD".
    era_year_pattern = r"\b([1-9][0-9]{0,3})\s*(BC|BCE|AD|CE)\b"
    for match in re.finditer(era_year_pattern, text, flags=re.IGNORECASE):
        raw_year = int(match.group(1))
        era = match.group(2).upper()
        signed_year = -raw_year if era in {"BC", "BCE"} else raw_year
        start, end = _historical_year_bounds(signed_year)
        ref = f"ti:year:{_signed_year(signed_year)}"
        found[ref] = TemporalMatch(
            time_ref_id=ref,
            time_kind="year",
            label=f"{raw_year} {era}",
            precision="year",
            start_date=start,
            end_date=end,
            year=signed_year,
            era_name=era,
            metadata_json={"source_pattern": "explicit_era_year", "era": era},
        )

    # Natural full dates: "February 12, 1809" or "12 February 1809"
    full_date_pattern = (
        r"\b("
        + "|".join(MONTHS.keys())
        + r")\s+([012]?[0-9]|3[01]),?\s+(1[0-9]{3}|20[0-9]{2})\b"
    )
    for match in re.finditer(full_date_pattern, text, flags=re.IGNORECASE):
        month_name = match.group(1).lower()
        day = int(match.group(2))
        year = int(match.group(3))
        month = MONTHS[month_name]
        if day < 1 or day > 31:
            continue
        date_s = f"{year:04d}-{month:02d}-{day:02d}"
        ref = f"tp:{date_s}"
        found[ref] = TemporalMatch(
            time_ref_id=ref,
            time_kind="point",
            label=f"{month_name.title()} {day}, {year}",
            precision="day",
            start_date=date_s,
            end_date=date_s,
            year=year,
            month=month,
            day=day,
            metadata_json={"source_pattern": "natural_full_date"},
        )

    # Day-first variant: "12 February 1809"
    day_first_pattern = (
        r"\b([012]?[0-9]|3[01])\s+("
        + "|".join(MONTHS.keys())
        + r")\s+(1[0-9]{3}|20[0-9]{2})\b"
    )
    for match in re.finditer(day_first_pattern, text, flags=re.IGNORECASE):
        day = int(match.group(1))
        month_name = match.group(2).lower()
        year = int(match.group(3))
        month = MONTHS[month_name]
        if day < 1 or day > 31:
            continue
        date_s = f"{year:04d}-{month:02d}-{day:02d}"
        ref = f"tp:{date_s}"
        if ref not in found:
            found[ref] = TemporalMatch(
                time_ref_id=ref,
                time_kind="point",
                label=f"{day} {month_name.title()} {year}",
                precision="day",
                start_date=date_s,
                end_date=date_s,
                year=year,
                month=month,
                day=day,
                metadata_json={"source_pattern": "natural_full_date_dmy"},
            )

    for match in re.finditer(r"\b(1[0-9]{3}|20[0-9]{2})-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])\b", text):
        date_s = match.group(0)
        ref = f"tp:{date_s}"
        found[ref] = TemporalMatch(
            time_ref_id=ref,
            time_kind="point",
            label=date_s,
            precision="day",
            start_date=date_s,
            end_date=date_s,
            year=int(match.group(1)),
            month=int(match.group(2)),
            day=int(match.group(3)),
            metadata_json={"source_pattern": "iso_date"},
        )

    month_pattern = r"\b(" + "|".join(MONTHS.keys()) + r")\s+(1[0-9]{3}|20[0-9]{2})\b"
    for match in re.finditer(month_pattern, text, flags=re.IGNORECASE):
        month_name = match.group(1).lower()
        year = int(match.group(2))
        month = MONTHS[month_name]
        start, end = _iso_month_bounds(year, month)
        ref = f"ti:month:{year:04d}-{month:02d}"
        found[ref] = TemporalMatch(
            time_ref_id=ref,
            time_kind="month",
            label=f"{month_name.title()} {year}",
            precision="month",
            start_date=start,
            end_date=end,
            year=year,
            month=month,
            metadata_json={"source_pattern": "month_year"},
        )

    season_pattern = r"\b(spring|summer|fall|autumn|winter)\s+(1[0-9]{3}|20[0-9]{2})\b"
    for match in re.finditer(season_pattern, text, flags=re.IGNORECASE):
        season = match.group(1).lower()
        year = int(match.group(2))
        start, end = _season_bounds(year, season)
        season_norm = "fall" if season == "autumn" else season
        ref = f"ti:season:{year:04d}:{season_norm}"
        found[ref] = TemporalMatch(
            time_ref_id=ref,
            time_kind="season",
            label=f"{season.title()} {year}",
            precision="season",
            start_date=start,
            end_date=end,
            year=year,
            season=season_norm,
            metadata_json={"source_pattern": "season_year"},
        )

    for match in re.finditer(r"\b(1[0-9]{3}|20[0-9]{2})\b", text):
        if _looks_like_coordinate_year(text, match.start(), match.end()):
            continue
        year = int(match.group(1))
        start, end = _iso_year_bounds(year)
        ref = f"ti:year:{year:04d}"
        if ref not in found:
            found[ref] = TemporalMatch(
                time_ref_id=ref,
                time_kind="year",
                label=str(year),
                precision="year",
                start_date=start,
                end_date=end,
                year=year,
                metadata_json={"source_pattern": "year"},
            )

    return list(found.values())
