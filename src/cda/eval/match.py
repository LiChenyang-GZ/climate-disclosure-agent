"""Page and text matching, independent of database records."""

import re
from typing import Protocol

from cda.parsing.units import normalize_text


class Location(Protocol):
    @property
    def document(self) -> str: ...
    @property
    def pdf_page(self) -> int: ...
    @property
    def quote(self) -> str: ...


def tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", normalize_text(text).lower()))


def token_overlap(found: str, key: str) -> bool:
    wanted = tokens(key)
    return bool(wanted) and len(tokens(found) & wanted) / len(wanted) >= 0.5


def evidence_matches(found: Location, key: Location) -> bool:
    if (found.document, found.pdf_page) != (key.document, key.pdf_page):
        return False
    a, b = normalize_text(found.quote).lower(), normalize_text(key.quote).lower()
    return bool(a and b) and (a in b or b in a or token_overlap(a, b))


# needs_review reasons that flag an inconsistency: an answer, not an abstention.
INCONSISTENCY_FLAGS = frozenset(
    {"possible_inconsistency", "regulatory_target_missing", "cross_reference_unclear", "t2_flag"}
)


def status_matches(status: str, key_status: str, reason: str | None = None) -> bool:
    if key_status == "inconsistent" and status == "needs_review":
        return reason in INCONSISTENCY_FLAGS
    return {
        "evidence_found": "found",
        "partial_evidence": "partial",
        "no_evidence_found": "not_found",
        "inconsistent": "inconsistent",
    }.get(status) == key_status
