from __future__ import annotations

import hashlib
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from cda.checks.models import CheckSet

REQUIRED_CHECK_IDS = (
    "G1",
    "G2",
    "S1",
    "S2",
    "S3",
    "S4",
    "S5",
    "R1",
    "M1",
    "M2",
    "M3",
    "A1",
    "A2",
    "A3",
    "A4",
)


class CheckSetError(ValueError):
    """Raised when a check-set file cannot be read or validated."""


def load_check_set(path: Path | None = None) -> CheckSet:
    if path is None:
        resource = files("cda.checks").joinpath("checks.yaml")
        source_name = str(resource)
        try:
            raw_bytes = resource.read_bytes()
        except OSError as error:
            raise CheckSetError(f"Could not read check set at {source_name}: {error}") from error
    else:
        source_name = str(path)
        try:
            raw_bytes = path.read_bytes()
        except FileNotFoundError as error:
            raise CheckSetError(f"Check-set file not found: {path}") from error
        except OSError as error:
            raise CheckSetError(f"Could not read check set at {path}: {error}") from error

    try:
        data: Any = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as error:
        raise CheckSetError(f"Invalid YAML in {source_name}: {error}") from error

    if not isinstance(data, dict):
        raise CheckSetError(f"Check set in {source_name} must be a mapping")
    if "sha256" in data:
        raise CheckSetError(
            "sha256 is generated from the file bytes and must not be supplied in YAML"
        )

    raw_checks = data.get("checks")
    if isinstance(raw_checks, list) and all(
        isinstance(check, dict) and isinstance(check.get("id"), str) for check in raw_checks
    ):
        raw_ids = [check["id"].strip() for check in raw_checks]
        if len(raw_ids) != len(set(raw_ids)):
            duplicate = next(check_id for check_id in raw_ids if raw_ids.count(check_id) > 1)
            raise CheckSetError(f"Duplicate check id: {duplicate}")

        actual_ids = set(raw_ids)
        required_ids = set(REQUIRED_CHECK_IDS)
        missing = sorted(required_ids - actual_ids)
        unexpected = sorted(actual_ids - required_ids)
        if missing or unexpected:
            details = []
            if missing:
                details.append(f"missing ids: {', '.join(missing)}")
            if unexpected:
                details.append(f"unexpected ids: {', '.join(unexpected)}")
            raise CheckSetError(
                "Check ids do not match REQUIRED_CHECK_IDS (" + "; ".join(details) + ")"
            )

    try:
        data["sha256"] = hashlib.sha256(raw_bytes).hexdigest()
        check_set = CheckSet.model_validate(data)
    except ValidationError as error:
        raise CheckSetError(f"Invalid check set in {source_name}: {error}") from error

    for check in check_set.checks:
        relief_ids = [relief.id for relief in check.allowed_reliefs]
        if len(relief_ids) != len(set(relief_ids)):
            duplicate = next(
                relief_id for relief_id in relief_ids if relief_ids.count(relief_id) > 1
            )
            raise CheckSetError(f"Check {check.id} has duplicate relief id: {duplicate}")

        fact_names = [fact.name for fact in check.facts]
        if len(fact_names) != len(set(fact_names)):
            duplicate = next(name for name in fact_names if fact_names.count(name) > 1)
            raise CheckSetError(f"Check {check.id} has duplicate fact name: {duplicate}")

        if not check.cross_document and check.inconsistency_types:
            raise CheckSetError(
                f"Check {check.id} has inconsistency_types but cross_document is false"
            )

    return check_set


@cache
def default_check_set() -> CheckSet:
    return load_check_set()
