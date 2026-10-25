"""Code-only inconsistency flags, applied on the caller's transaction."""

import re
from itertools import combinations
from pathlib import PurePath
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cda.checks.loader import default_check_set
from cda.compare.facts import load_evidence, load_facts
from cda.compare.normalise import normalise_scope, normalise_unit, reporting_period
from cda.db.models import AuditLog, Document, DocumentPart, Finding, Inconsistency, Run


def _normalise_reference(text: str) -> str:
    return " ".join(text.lower().replace("-", " ").replace("_", " ").split())


def _reference_names(
    filename: str, label: str | None, document_kind: str, part_kind: str | None
) -> set[str]:
    names = {PurePath(filename).stem, label or ""}
    kinds = {document_kind, part_kind}
    if "annual" in kinds:
        names.add("annual report")
    if "sustainability" in kinds:
        names.update(
            {
                "sustainability report",
                "climate report",
                "climate-related financial disclosure",
                "climate-related financial disclosures",
                "climate statement",
            }
        )
    return {normalised for name in names if (normalised := _normalise_reference(name))}


def side(fact: dict[str, Any]) -> dict[str, Any]:
    unit = normalise_unit(fact.get("unit"))
    value = fact.get("value_num")
    return {
        "fact_id": str(fact["id"]) if "name" in fact else None,
        "unit_id": str(fact["unit_id"]),
        "document_id": str(fact["document_id"]),
        "part_id": fact["part_id"],
        "part_kind": fact["part_kind"],
        "pdf_page": fact["pdf_page"],
        "raw": fact.get("raw", fact.get("quote")),
        "normalised_value": str(value * unit[1]) if value is not None and unit else None,
        "normalised_unit": unit[0] if unit else None,
        "scope": normalise_scope(fact.get("scope")),
        "year": fact.get("year"),
    }


async def compare_run(conn: AsyncConnection, run_id: UUID) -> list[UUID]:
    facts = await load_facts(conn, run_id)
    findings = {
        r["id"]: dict(r)
        for r in (
            await conn.execute(
                select(Finding).where(Finding.run_id == run_id).order_by(Finding.check_id)
            )
        ).mappings()
    }
    previous_t2 = set(
        (
            await conn.execute(
                select(Inconsistency.finding_id).where(
                    Inconsistency.run_id == run_id, Inconsistency.type == "T2"
                )
            )
        ).scalars()
    )
    await conn.execute(delete(Inconsistency).where(Inconsistency.run_id == run_id))
    rows: list[dict[str, Any]] = []
    changes: dict[UUID, tuple[str, str | None, str]] = {}

    def flag(
        kind: str,
        a: dict[str, Any],
        b: dict[str, Any] | None,
        reason: str,
        fid: UUID | None,
        **extra: Any,
    ) -> None:
        identity = f"{kind}:{fid}:{a['id']}:{b['id'] if b else ''}"
        rows.append(
            {
                "id": uuid5(run_id, identity),
                "run_id": run_id,
                "type": kind,
                "finding_id": fid,
                "detail": {
                    "side_a": side(a),
                    "side_b": side(b) if b else None,
                    "reason": reason,
                    **extra,
                },
            }
        )

    def request(fid: UUID, status: str, reason: str | None, kind: str) -> None:
        old = findings[fid]["status"]
        if old in {"error", "not_checked"}:
            return
        if old == "inconsistent" and status != "inconsistent" and kind != "T2":
            return
        if fid in changes and changes[fid][0] == "inconsistent":
            return
        if status == "needs_review" and old not in {
            "evidence_found",
            "partial_evidence",
            "no_evidence_found",
            "inconsistent",
        }:
            return
        changes[fid] = (status, reason, kind)

    for fid, finding in findings.items():
        check = finding["check_id"]
        if check == "A2" and finding["status"] == "inconsistent":
            evidence = await load_evidence(conn, fid)
            a = next((e for e in evidence if e["role"] == "side_a"), None)
            b = next((e for e in evidence if e["role"] == "side_b"), None)
            if a and b:
                flag("T1", a, b, "Worker flagged differing passages", fid)
        if check == "S4" and (finding["status"] == "inconsistent" or fid in previous_t2):
            evidence = await load_evidence(conn, fid)
            a = next((e for e in evidence if e["role"] == "side_a"), None)
            b = next((e for e in evidence if e["role"] == "side_b"), None)
            # A worker-submitted inconsistent finding normally has both sides.
            detail = {
                "side_a": side(a) if a else None,
                "side_b": side(b) if b else None,
                "reason": "Worker flagged financial assumptions for review",
            }
            rows.append(
                {
                    "id": uuid5(run_id, f"T2:{fid}"),
                    "run_id": run_id,
                    "type": "T2",
                    "finding_id": fid,
                    "detail": detail,
                }
            )
            request(fid, "needs_review", "t2_flag", "T2")

    eligible = {c.id for c in default_check_set().checks if "T3" in c.inconsistency_types}
    for a, b in combinations(facts, 2):
        if a["part_kind"] == b["part_kind"]:
            continue
        fid = a["finding_id"]
        if a["name"] == b["name"] == "reporting_period":
            pa, pb = reporting_period(a["raw"]), reporting_period(b["raw"])
            if pa and pb and pa != pb:
                flag(
                    "T6", a, b, "Reporting period ends differ", None, end_a=list(pa), end_b=list(pb)
                )
            continue
        if a["check_id"] not in eligible or b["check_id"] not in eligible:
            continue
        if a["name"] != b["name"]:
            continue
        ua, ub = normalise_unit(a["unit"]), normalise_unit(b["unit"])
        va, vb = a["value_num"], b["value_num"]
        same_unit = ua is not None and ub is not None and ua[0] == ub[0]
        if same_unit and va is not None and vb is not None:
            assert ua is not None and ub is not None
            va, vb = va * ua[1], vb * ub[1]
            if abs(va - vb) <= max(abs(va), abs(vb)) / 100:
                continue
        elif va == vb and a["raw"] == b["raw"]:
            continue
        sa, sb = normalise_scope(a["scope"]), normalise_scope(b["scope"])
        comparable = (
            same_unit
            and va is not None
            and vb is not None
            and sa is not None
            and sa == sb
            and a["year"] is not None
            and a["year"] == b["year"]
        )
        flag(
            "T3",
            a,
            b,
            "Extracted figures differ"
            if comparable
            else "Differing figures lack matching unit, scope or year",
            fid,
            comparable=comparable,
        )
        for owner in {fid, b["finding_id"]}:
            request(
                owner,
                "inconsistent" if comparable else "needs_review",
                None if comparable else "possible_inconsistency",
                "T3",
            )

    m3 = [f for f in facts if f["check_id"] == "M3"]
    targets = any(
        f["part_kind"] == "sustainability"
        and f["name"] in {"safeguard_baseline", "emissions_target"}
        for f in m3
    )
    if not targets:
        for a in m3:
            if a["part_kind"] == "annual" and a["name"] == "safeguard_baseline":
                flag(
                    "T4",
                    a,
                    None,
                    "Regulatory baseline missing from sustainability targets",
                    a["finding_id"],
                )
                request(a["finding_id"], "needs_review", "regulatory_target_missing", "T4")

    names = (
        await conn.execute(
            select(Document.filename, DocumentPart.label, Document.kind, DocumentPart.kind)
            .outerjoin(DocumentPart, DocumentPart.document_id == Document.id)
            .where(
                Document.pack_id == select(Run.pack_id).where(Run.id == run_id).scalar_subquery(),
                Document.deleted_at.is_(None),
            )
        )
    ).all()
    labels = {
        name
        for filename, label, document_kind, part_kind in names
        for name in _reference_names(filename, label, document_kind, part_kind)
    }
    for a in facts:
        if a["check_id"] != "A3" or a["name"] != "cross_reference_target":
            continue
        raw = _normalise_reference(a["raw"])
        document = any(
            re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", raw) for label in labels
        )
        location = re.search(
            r"\b(?:section|page|note|chapter|part)\b|\bp\.|\b\d+\.\d+\b", a["raw"].lower()
        )
        if not document or not location:
            flag("T5", a, None, "Reference lacks a known document or location", a["finding_id"])
            request(a["finding_id"], "needs_review", "cross_reference_unclear", "T5")

    rows.sort(
        key=lambda row: (
            row["type"],
            findings.get(row["finding_id"], {}).get("check_id", ""),
            str(row["id"]),
        )
    )
    for row in rows:
        await conn.execute(insert(Inconsistency).values(**row))
    for fid, (status, reason, kind) in sorted(
        changes.items(), key=lambda item: findings[item[0]]["check_id"]
    ):
        old = findings[fid]["status"]
        if old == status:
            continue
        await conn.execute(
            update(Finding)
            .where(Finding.id == fid)
            .values(
                status=status,
                needs_review_reason=reason,
                version=Finding.version + 1,
                updated_at=func.now(),
            )
        )
        await conn.execute(
            insert(AuditLog).values(
                actor="system",
                action="status_changed_by_compare",
                target=str(fid),
                detail={"old_status": old, "new_status": status, "inconsistency_type": kind},
            )
        )
    return [row["id"] for row in rows]
