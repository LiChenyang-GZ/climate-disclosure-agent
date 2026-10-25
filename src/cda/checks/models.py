from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Pillar = Literal["governance", "strategy", "risk_management", "metrics_targets", "asic_review"]
DocumentKind = Literal["sustainability", "annual", "other"]
InconsistencyType = Literal["T1", "T2", "T3", "T4", "T5"]


def normalise_query(text: str) -> str:
    """Return a query with lowercase text and collapsed whitespace."""
    return re.sub(r"\s+", " ", text.strip().lower())


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("*", mode="before")
    @classmethod
    def strip_strings(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return [item.strip() if isinstance(item, str) else item for item in value]
        return value

    @field_validator("*", mode="after")
    @classmethod
    def reject_empty_strings(cls, value: object) -> object:
        if isinstance(value, str) and not value:
            raise ValueError("string must be non-empty")
        if isinstance(value, list) and any(isinstance(item, str) and not item for item in value):
            raise ValueError("strings must be non-empty")
        return value


class Source(FrozenModel):
    standard: str
    references: list[str] = Field(min_length=1)
    asic: str | None = None
    law: str | None = None


class Relief(FrozenModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str


class FactSpec(FrozenModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    unit: str
    description: str


class Check(FrozenModel):
    id: str = Field(pattern=r"^[GSRMA][0-9]$")
    title: str
    pillar: Pillar
    source: Source
    requirement: str
    counts_as_evidence: list[str] = Field(min_length=1)
    does_not_count: list[str] = Field(min_length=1)
    allowed_reliefs: list[Relief]
    facts: list[FactSpec]
    preferred_documents: list[DocumentKind] = Field(min_length=1)
    queries: list[str] = Field(min_length=2, max_length=3)
    cross_document: bool
    inconsistency_types: list[InconsistencyType]
    min_searches: int | None = Field(default=None, ge=1)

    @field_validator("preferred_documents")
    @classmethod
    def preferred_documents_are_unique(cls, value: list[DocumentKind]) -> list[DocumentKind]:
        if len(value) != len(set(value)):
            raise ValueError("preferred_documents must not contain duplicates")
        return value

    @field_validator("queries")
    @classmethod
    def queries_are_unique(cls, value: list[str]) -> list[str]:
        normalised = [normalise_query(query) for query in value]
        if len(normalised) != len(set(normalised)):
            raise ValueError("queries must not contain duplicates after normalisation")
        return value


class Defaults(FrozenModel):
    min_searches: int = Field(ge=1)


class CheckSet(FrozenModel):
    version: str
    defaults: Defaults
    checks: list[Check]
    sha256: str

    def get(self, check_id: str) -> Check:
        for check in self.checks:
            if check.id == check_id:
                return check
        raise KeyError(f"Unknown check id: {check_id}")

    def ids(self) -> list[str]:
        return [check.id for check in self.checks]

    def min_searches_for(self, check_id: str) -> int:
        check = self.get(check_id)
        return check.min_searches if check.min_searches is not None else self.defaults.min_searches
