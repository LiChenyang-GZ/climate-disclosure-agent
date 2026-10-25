"""Validated document part configuration and persistence."""

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Self
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from cda.db.enums import DocumentKind
from cda.db.models import Document, DocumentPart


class Part(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: DocumentKind
    first_pdf_page: int = Field(ge=1)
    last_pdf_page: int
    label: str | None = None

    @model_validator(mode="after")
    def valid_range(self) -> Self:
        if self.last_pdf_page < self.first_pdf_page:
            raise ValueError("last_pdf_page must be >= first_pdf_page")
        return self


class PartsDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str
    kind: DocumentKind
    parts: list[Part] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_parts(self) -> Self:
        ordered = sorted(self.parts, key=lambda p: p.first_pdf_page)
        for left, right in itertools.pairwise(ordered):
            if right.first_pdf_page <= left.last_pdf_page:
                raise ValueError(
                    f"overlapping ranges {left.first_pdf_page}-{left.last_pdf_page} and {right.first_pdf_page}-{right.last_pdf_page}"
                )
        return self


class PartsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    documents: list[PartsDocument]

    @model_validator(mode="after")
    def valid_documents(self, info: ValidationInfo) -> Self:
        names = [d.filename for d in self.documents]
        if len(set(names)) != len(names):
            raise ValueError("duplicate filenames")
        if info.context:
            folder: Path = info.context["folder"]
            for name in names:
                if Path(name).name != name or not (folder / name).is_file():
                    raise ValueError(f"PDF does not exist next to YAML: {name}")
        return self


def load_parts_file(path: str | Path) -> PartsFile:
    path = Path(path)
    return PartsFile.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8")), context={"folder": path.parent}
    )


class PartRange(Protocol):
    @property
    def kind(self) -> DocumentKind: ...
    @property
    def first_pdf_page(self) -> int: ...
    @property
    def last_pdf_page(self) -> int: ...
    @property
    def label(self) -> str | None: ...


@dataclass(frozen=True)
class ResolvedPart:
    kind: DocumentKind
    part_id: UUID | None
    label: str | None


def resolve_part(
    parts: Sequence[PartRange], document_kind: DocumentKind, pdf_page: int
) -> ResolvedPart:
    for part in parts:
        if part.first_pdf_page <= pdf_page <= part.last_pdf_page:
            return ResolvedPart(part.kind, getattr(part, "id", None), part.label)
    return ResolvedPart(document_kind, None, None)


async def replace_parts(conn: AsyncConnection, document_id: UUID, parts: Sequence[Part]) -> None:
    page_count = (
        await conn.execute(select(Document.page_count).where(Document.id == document_id))
    ).scalar_one()
    if any(p.last_pdf_page > page_count for p in parts):
        raise ValueError("part exceeds document page_count")
    # Reuse the same overlap validation for programmatically supplied configuration.
    PartsDocument(filename="", kind="other", parts=list(parts))
    async with conn.begin_nested():
        await conn.execute(delete(DocumentPart).where(DocumentPart.document_id == document_id))
        if parts:
            await conn.execute(
                insert(DocumentPart),
                [dict(document_id=document_id, **p.model_dump()) for p in parts],
            )
