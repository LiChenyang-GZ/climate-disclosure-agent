"""Portable, validated reference labels for the synthetic pack."""

from datetime import date
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from cda.checks.loader import load_check_set
from cda.parsing.parts import load_parts_file, resolve_part

Difficulty = Literal["direct", "paraphrase", "split", "table"]
PartKind = Literal["annual", "sustainability", "other"]
CheckId = str


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
    relief_applied: str | None = None
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
    type: Literal["T1", "T2", "T3", "T4", "T5", "T6", "disclaimer", "single_scenario", "injection"]
    check_id: CheckId | None = None
    locations: list[Evidence] = Field(min_length=1)


class Document(StrictModel):
    filename: str
    kind: PartKind


class Change(StrictModel):
    date: date
    cell: str
    before: str
    after: str
    reason: str


class AnswerKey(StrictModel):
    key_version: int = Field(default=1, ge=1)
    company: str = "Wattlebrook Energy Limited"
    labeler: Literal["generator", "claude", "christy"] = "generator"
    index_table: bool = False
    changes: list[Change] = Field(default_factory=list)
    documents: list[Document]
    cells: list[Cell]
    planted_issues: list[Issue] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # Preserve the committed T15 generator's byte-for-byte YAML output.
        data: dict[str, Any] = handler(self)
        if not self.index_table:
            data.pop("index_table", None)
        if not self.changes:
            data.pop("changes", None)
        return data

    @model_validator(mode="after")
    def complete(self) -> Self:
        checks = load_check_set()
        expected = set(checks.ids())
        if len(self.cells) != 15 or {c.check_id for c in self.cells} != expected:
            raise ValueError("each of the fifteen checks must appear exactly once")
        names = {d.filename for d in self.documents}
        for issue in self.planted_issues:
            if issue.check_id is not None and issue.check_id not in expected:
                raise ValueError("unknown issue check")
            if any(e.document not in names for e in issue.locations):
                raise ValueError("unknown issue document")
        for c in self.cells:
            if c.relief_applied is not None and c.relief_applied not in {
                r.id for r in checks.get(c.check_id).allowed_reliefs
            }:
                raise ValueError("relief not allowed for check")
            if any(e.document not in names for e in c.evidence + c.scanned):
                raise ValueError("unknown document")
            if (
                c.status in ("not_found", "inconsistent")
                and {s.document for s in c.scanned} != names
            ):
                raise ValueError("scans must cover every document")
        return self


def load_answer_key(path: str | Path) -> AnswerKey:
    path = Path(path)
    key = AnswerKey.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    parts = {d.filename: d for d in load_parts_file(path.parent / "parts.yaml").documents}
    if set(parts) != {d.filename for d in key.documents}:
        raise ValueError("parts documents disagree with key")
    evidence = [e for c in key.cells for e in c.evidence] + [
        e for i in key.planted_issues for e in i.locations
    ]
    for e in evidence:
        doc = parts[e.document]
        if resolve_part(doc.parts, doc.kind, e.pdf_page).kind != e.part:
            raise ValueError("evidence part disagrees with parts.yaml")
    return key
