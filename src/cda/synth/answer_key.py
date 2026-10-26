"""Portable, validated reference labels for the synthetic pack."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Difficulty = Literal["direct", "paraphrase", "split", "table"]
PartKind = Literal["annual", "sustainability", "other"]
CheckId = Literal[
    "G1", "G2", "S1", "S2", "S3", "S4", "S5", "R1", "M1", "M2", "M3", "A1", "A2", "A3", "A4"
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(StrictModel):
    document: str
    part: PartKind
    pdf_page: int = Field(ge=1)
    quote: str = Field(min_length=1)
    role: Literal["primary", "alternate"] = "primary"
    source_type: Literal["text", "table", "figure"] = "text"
    difficulty: Difficulty = "direct"


class Scan(StrictModel):
    document: str
    first_pdf_page: int = Field(ge=1)
    last_pdf_page: int

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.last_pdf_page < self.first_pdf_page:
            raise ValueError("scan range is reversed")
        return self


class Cell(StrictModel):
    check_id: CheckId
    status: Literal["found", "partial", "not_found", "inconsistent", "not_applicable"]
    relief_applied: (
        Literal["scope3_first_year", "no_prior_period_progress", "not_separately_identifiable"]
        | None
    ) = None
    difficulty: Difficulty | None
    unsure: bool = False
    evidence: list[Evidence] = Field(default_factory=list)
    scanned: list[Scan] = Field(default_factory=list)

    @model_validator(mode="after")
    def coverage(self) -> Self:
        if self.status in ("not_found", "inconsistent") and not self.scanned:
            raise ValueError("absence and inconsistencies require scanned ranges")
        if self.status == "not_found" and self.evidence:
            raise ValueError("absence cannot have evidence")
        return self


class Issue(StrictModel):
    id: str
    type: Literal["T1", "T3", "T4", "T5", "disclaimer", "single_scenario", "injection"]
    check_id: CheckId | None = None
    locations: list[Evidence] = Field(min_length=1)


class Document(StrictModel):
    filename: str
    kind: PartKind


class AnswerKey(StrictModel):
    key_version: Literal[1] = 1
    company: Literal["Wattlebrook Energy Limited"] = "Wattlebrook Energy Limited"
    labeler: Literal["generator"] = "generator"
    documents: list[Document]
    cells: list[Cell]
    planted_issues: list[Issue]

    @model_validator(mode="after")
    def complete(self) -> Self:
        expected = {
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
        }
        if len(self.cells) != 15 or {c.check_id for c in self.cells} != expected:
            raise ValueError("each of the fifteen checks must appear exactly once")
        names = {d.filename for d in self.documents}
        for c in self.cells:
            if any(e.document not in names for e in c.evidence + c.scanned):
                raise ValueError("unknown document")
            if (
                c.status in ("not_found", "inconsistent")
                and {s.document for s in c.scanned} != names
            ):
                raise ValueError("scans must cover every document")
        return self
