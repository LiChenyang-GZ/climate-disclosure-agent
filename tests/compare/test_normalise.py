from decimal import Decimal

import pytest

from cda.compare.normalise import normalise_scope, normalise_unit, reporting_period


@pytest.mark.parametrize(
    "unit,canonical,factor",
    [
        *[(u, "tCO2e", 1) for u in ["t", "tCO2e", "tCO2-e", "t CO2e", "tonnes CO2e"]],
        ("kt", "tCO2e", 1000),
        ("kt CO2-e", "tCO2e", 1000),
        ("MtCO2e", "tCO2e", 1000000),
        *[(u, "AUD", 1) for u in ["AUD", "$", "A$"]],
        *[
            (f"{currency} {u}", "AUD", n)
            for currency in ["AUD", "$", "A$"]
            for u, n in [
                ("thousand", 1000),
                ("'000", 1000),
                ("k", 1000),
                ("m", 1000000),
                ("mn", 1000000),
                ("million", 1000000),
                ("bn", 1000000000),
                ("billion", 1000000000),
            ]
        ],
        *[(u, "percent", 1) for u in ["%", "percent", "pct"]],
        *[(u, "degC", 1) for u in ["degC", "°C"]],
    ],
)
def test_units(unit, canonical, factor):
    assert normalise_unit(unit) == (canonical, Decimal(factor))


def test_unknown():
    assert normalise_unit("widgets") is None


@pytest.mark.parametrize(
    "raw,result",
    [
        ("FY2026", (2026, 6)),
        ("FY26", (2026, 6)),
        ("year ended 30 June 2026", (2026, 6)),
        ("1 July 2025 – 30 June 2026", (2026, 6)),
        ("2025-07-01 to 2026-06-30", (2026, 6)),
        ("garbage", None),
        ("year ended 31 June 2026", None),
        ("2026-13-30", None),
    ],
)
def test_period(raw, result):
    assert reporting_period(raw) == result


def test_scope():
    assert normalise_scope(" GROUP  Operations ") == "group operations"
    assert normalise_scope(" ") is None
