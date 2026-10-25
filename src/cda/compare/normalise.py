"""Conservative, decimal-preserving comparison normalisation."""

import calendar
import re
from datetime import date
from decimal import Decimal


def normalise_unit(unit: str | None) -> tuple[str, Decimal] | None:
    text = re.sub(r"\s+", "", unit or "").lower().replace("co2-e", "co2e")
    emission = re.fullmatch(r"(t|kt|mt|tonnes)(?:co2e)?", text)
    if emission:
        return "tCO2e", Decimal({"t": 1, "tonnes": 1, "kt": 1000, "mt": 1000000}[emission[1]])
    if text in {"%", "percent", "pct"}:
        return "percent", Decimal(1)
    if text in {"degc", "°c"}:
        return "degC", Decimal(1)
    money = re.fullmatch(r"(?:aud|a\$|\$)(thousand|\x27000|k|m|mn|million|bn|billion)?", text)
    # Multiplier-first forms (e.g. million AUD) express the same currency.
    if money is None:
        money = re.fullmatch(r"(thousand|\x27000|k|m|mn|million|bn|billion)(?:aud|a\$|\$)", text)
    if money:
        factor = {
            "": 1,
            "thousand": 1000,
            "'000": 1000,
            "k": 1000,
            "m": 1000000,
            "mn": 1000000,
            "million": 1000000,
            "bn": 1000000000,
            "billion": 1000000000,
        }[money[1] or ""]
        return "AUD", Decimal(factor)
    return None


def normalise_scope(scope: str | None) -> str | None:
    return " ".join((scope or "").lower().split()) or None


def reporting_period(raw: str) -> tuple[int, int] | None:
    text = raw.strip().lower()
    fy = re.fullmatch(r"fy\s*(\d{4}|\d{2})", text)
    if fy:
        year = int(fy[1])
        return (2000 + year if len(fy[1]) == 2 else year), 6
    dates = re.findall(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if dates:
        year, month, day = map(int, dates[-1])
    else:
        named = re.findall(r"\b(\d{1,2})\s+([a-z]+)\s+(\d{4})\b", text)
        if not named:
            return None
        day_text, month_text, year_text = named[-1]
        months = {name.lower(): i for i, name in enumerate(calendar.month_name) if i}
        if month_text not in months:
            return None
        year, month, day = int(year_text), months[month_text], int(day_text)
    try:
        date(year, month, day)
    except ValueError:
        return None
    return year, month
