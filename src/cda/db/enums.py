"""Allowed database values, shared by schema and application validation."""

from typing import Literal, get_args

UserRole = Literal["lead", "analyst"]
USER_ROLES: tuple[str, ...] = get_args(UserRole)

DocumentKind = Literal["sustainability", "annual", "other"]
DOCUMENT_KINDS: tuple[str, ...] = get_args(DocumentKind)

UnitKind = Literal["paragraph", "table_row", "section", "table"]
UNIT_KINDS: tuple[str, ...] = get_args(UnitKind)

RunMethod = Literal["A", "B", "C", "LC"]
RUN_METHODS: tuple[str, ...] = get_args(RunMethod)

RunStatus = Literal["queued", "running", "waiting_review", "final", "cancelled", "failed"]
RUN_STATUSES: tuple[str, ...] = get_args(RunStatus)

FindingStatus = Literal[
    "evidence_found",
    "partial_evidence",
    "no_evidence_found",
    "inconsistent",
    "needs_review",
    "not_checked",
    "error",
]
FINDING_STATUSES: tuple[str, ...] = get_args(FindingStatus)

Confidence = Literal["high", "low"]
CONFIDENCES: tuple[str, ...] = get_args(Confidence)

EvidenceRole = Literal["supports", "side_a", "side_b"]
EVIDENCE_ROLES: tuple[str, ...] = get_args(EvidenceRole)

InconsistencyType = Literal["T1", "T2", "T3", "T4", "T5", "T6"]
INCONSISTENCY_TYPES: tuple[str, ...] = get_args(InconsistencyType)

ReviewDecision = Literal["confirm", "change_status"]
REVIEW_DECISIONS: tuple[str, ...] = get_args(ReviewDecision)
