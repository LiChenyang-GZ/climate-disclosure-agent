from __future__ import annotations

import pytest
from pydantic import ValidationError

from cda.checks.loader import load_check_set
from cda.checks.models import Check, normalise_query


def test_models_are_frozen() -> None:
    check = load_check_set().get("G1")

    with pytest.raises(ValidationError):
        check.title = "Changed title"  # type: ignore[misc]


def test_normalise_query() -> None:
    assert normalise_query("  Scenario   ANALYSIS ") == "scenario analysis"


def test_check_rejects_invalid_values() -> None:
    payload = load_check_set().get("G1").model_dump()
    payload["requirement"] = "   "

    with pytest.raises(ValidationError, match="requirement"):
        Check.model_validate(payload)
