from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from cda.checks.loader import REQUIRED_CHECK_IDS, CheckSetError, load_check_set

CHECKS_PATH = Path(__file__).parents[2] / "src" / "cda" / "checks" / "checks.yaml"


@pytest.fixture
def yaml_data() -> dict[str, Any]:
    return yaml.safe_load(CHECKS_PATH.read_bytes())


def write_data(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "checks.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_packaged_check_set_loads_with_raw_file_hash() -> None:
    check_set = load_check_set()

    assert len(check_set.checks) == 15
    assert check_set.ids() == list(REQUIRED_CHECK_IDS)
    assert check_set.version == "2026.10.1"
    assert len(check_set.sha256) == 64
    assert check_set.sha256 == hashlib.sha256(CHECKS_PATH.read_bytes()).hexdigest()


def test_min_searches_override_and_default() -> None:
    check_set = load_check_set()

    assert check_set.min_searches_for("S5") == 6
    assert check_set.min_searches_for("G1") == 4


def test_get_returns_checks_and_names_unknown_id() -> None:
    check_set = load_check_set()

    assert "scope3_first_year" in {relief.id for relief in check_set.get("M1").allowed_reliefs}
    assert check_set.get("A2").inconsistency_types == ["T1"]
    with pytest.raises(KeyError, match="X9"):
        check_set.get("X9")


def test_duplicate_check_id_is_reported(yaml_data: dict[str, Any], tmp_path: Path) -> None:
    yaml_data["checks"][1]["id"] = "G1"

    with pytest.raises(CheckSetError, match="G1"):
        load_check_set(write_data(tmp_path, yaml_data))


def test_missing_check_id_is_reported(yaml_data: dict[str, Any], tmp_path: Path) -> None:
    yaml_data["checks"] = [check for check in yaml_data["checks"] if check["id"] != "A4"]

    with pytest.raises(CheckSetError, match="A4"):
        load_check_set(write_data(tmp_path, yaml_data))


def test_unexpected_check_id_is_reported(yaml_data: dict[str, Any], tmp_path: Path) -> None:
    yaml_data["checks"][-1]["id"] = "Z1"

    with pytest.raises(CheckSetError, match="Z1"):
        load_check_set(write_data(tmp_path, yaml_data))


def test_unknown_check_field_is_reported(yaml_data: dict[str, Any], tmp_path: Path) -> None:
    yaml_data["checks"][0]["foo"] = "bar"

    with pytest.raises(CheckSetError, match="foo"):
        load_check_set(write_data(tmp_path, yaml_data))


@pytest.mark.parametrize("queries", [["only one"], ["one", "two", "three", "four"]])
def test_query_count_must_be_two_or_three(
    yaml_data: dict[str, Any], tmp_path: Path, queries: list[str]
) -> None:
    yaml_data["checks"][0]["queries"] = queries

    with pytest.raises(CheckSetError, match="queries"):
        load_check_set(write_data(tmp_path, yaml_data))


def test_queries_cannot_duplicate_after_normalisation(
    yaml_data: dict[str, Any], tmp_path: Path
) -> None:
    yaml_data["checks"][0]["queries"] = ["Scope 1", "  scope   1 "]

    with pytest.raises(CheckSetError, match="queries"):
        load_check_set(write_data(tmp_path, yaml_data))


def test_min_searches_must_be_positive(yaml_data: dict[str, Any], tmp_path: Path) -> None:
    yaml_data["checks"][0]["min_searches"] = 0

    with pytest.raises(CheckSetError, match="min_searches"):
        load_check_set(write_data(tmp_path, yaml_data))


def test_preferred_documents_must_use_document_kind(
    yaml_data: dict[str, Any], tmp_path: Path
) -> None:
    yaml_data["checks"][0]["preferred_documents"] = ["pdf"]

    with pytest.raises(CheckSetError, match="preferred_documents"):
        load_check_set(write_data(tmp_path, yaml_data))


def test_t6_is_not_a_check_inconsistency_type(yaml_data: dict[str, Any], tmp_path: Path) -> None:
    yaml_data["checks"][0]["cross_document"] = True
    yaml_data["checks"][0]["inconsistency_types"] = ["T6"]

    with pytest.raises(CheckSetError, match="inconsistency_types"):
        load_check_set(write_data(tmp_path, yaml_data))


def test_non_cross_document_check_cannot_have_inconsistencies(
    yaml_data: dict[str, Any], tmp_path: Path
) -> None:
    check = yaml_data["checks"][0]
    check["inconsistency_types"] = ["T1"]

    with pytest.raises(CheckSetError, match=check["id"]):
        load_check_set(write_data(tmp_path, yaml_data))


def test_relief_ids_must_be_unique_per_check(yaml_data: dict[str, Any], tmp_path: Path) -> None:
    reliefs = yaml_data["checks"][8]["allowed_reliefs"]
    reliefs[1]["id"] = reliefs[0]["id"]

    with pytest.raises(CheckSetError, match=reliefs[0]["id"]):
        load_check_set(write_data(tmp_path, yaml_data))


def test_fact_names_must_be_unique_per_check(yaml_data: dict[str, Any], tmp_path: Path) -> None:
    facts = yaml_data["checks"][8]["facts"]
    facts[1]["name"] = facts[0]["name"]

    with pytest.raises(CheckSetError, match=facts[0]["name"]):
        load_check_set(write_data(tmp_path, yaml_data))


def test_blank_requirement_is_rejected(yaml_data: dict[str, Any], tmp_path: Path) -> None:
    yaml_data["checks"][0]["requirement"] = "   "

    with pytest.raises(CheckSetError, match="requirement"):
        load_check_set(write_data(tmp_path, yaml_data))


def test_invalid_yaml_is_wrapped(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("checks: [\n", encoding="utf-8")

    with pytest.raises(CheckSetError) as error:
        load_check_set(path)

    assert isinstance(error.value.__cause__, yaml.YAMLError)


def test_missing_file_error_names_path(tmp_path: Path) -> None:
    path = tmp_path / "missing.yaml"

    with pytest.raises(CheckSetError, match=re.escape(str(path))):
        load_check_set(path)


def test_changing_file_content_changes_sha256(tmp_path: Path) -> None:
    raw = CHECKS_PATH.read_bytes()
    changed = raw.replace(b"2026.10.1", b"2026.10.2", 1)
    path = tmp_path / "checks.yaml"
    path.write_bytes(changed)

    assert load_check_set(path).sha256 != load_check_set().sha256
