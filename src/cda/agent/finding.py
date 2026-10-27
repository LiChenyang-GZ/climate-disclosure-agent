"""Strict submission schema and check-specific rules."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cda.checks.models import Check


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceArgs(Args):
    unit_id: str
    quote: str = Field(min_length=1)
    role: Literal["supports", "side_a", "side_b"]

    @field_validator("unit_id")
    @classmethod
    def uuid_string(cls, value: str) -> str:
        UUID(value)
        return value


class FactArgs(Args):
    name: str
    value: float | None = Field(allow_inf_nan=False)
    unit: str | None
    scope: str | None
    year: int | None
    unit_id: str
    raw: str

    @field_validator("unit_id")
    @classmethod
    def uuid_string(cls, value: str) -> str:
        UUID(value)
        return value


class SubmitFinding(Args):
    status: Literal["evidence_found", "partial_evidence", "no_evidence_found", "inconsistent"]
    rationale: str = Field(min_length=1, max_length=2000)
    missing_aspects: list[str]
    relief_applied: bool
    relief_id: str | None
    evidence: list[EvidenceArgs] = Field(max_length=10)
    facts: list[FactArgs] = Field(max_length=20)

    def drop_uncited_facts(self) -> int:
        """Drop facts whose unit is not in the evidence list; return how many were dropped.

        Uncited facts made check_rules reject the whole finding (most invalid_output
        errors in the 27 Oct dev-set runs). No-evidence findings keep their own rule.
        """
        if self.status == "no_evidence_found":
            return 0
        ids = {UUID(e.unit_id) for e in self.evidence}
        kept = [f for f in self.facts if UUID(f.unit_id) in ids]
        dropped = len(self.facts) - len(kept)
        self.facts = kept
        return dropped

    def check_rules(self, check: Check) -> None:
        if self.status in {"evidence_found", "partial_evidence"} and not self.evidence:
            raise ValueError("Evidence required")
        if self.status == "no_evidence_found" and (self.evidence or self.facts):
            raise ValueError("No evidence status forbids evidence and facts")
        if self.status == "inconsistent" and not {"side_a", "side_b"} <= {
            e.role for e in self.evidence
        }:
            raise ValueError("Both sides required")
        if self.status == "partial_evidence" and not self.missing_aspects:
            raise ValueError("Missing aspects required")
        if self.relief_applied != (self.relief_id is not None):
            raise ValueError("Relief flag and id must agree")
        if self.relief_id is not None and self.relief_id not in {
            r.id for r in check.allowed_reliefs
        }:
            raise ValueError("Relief not allowed")
        ids = {UUID(e.unit_id) for e in self.evidence}
        for fact in self.facts:
            if fact.name not in {f.name for f in check.facts}:
                raise ValueError("Fact name not allowed")
            if UUID(fact.unit_id) not in ids:
                raise ValueError("Fact must cite evidence")
