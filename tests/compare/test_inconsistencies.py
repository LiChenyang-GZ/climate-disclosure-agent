from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select, update

from cda.compare import compare_run
from cda.db.models import (
    AuditLog,
    Document,
    DocumentPart,
    Evidence,
    Fact,
    Finding,
    Inconsistency,
    Unit,
)

pytestmark = pytest.mark.db


async def setup(env, check="M1", status="evidence_found"):
    engine, rid, _, llm, _ = env
    async with engine.begin() as c:
        doc = await c.scalar(select(Unit.document_id).where(Unit.id == llm.unit))
        await c.execute(update(Document).where(Document.id == doc).values(page_count=10))
        await c.execute(
            insert(DocumentPart).values(
                document_id=doc,
                kind="sustainability",
                first_pdf_page=1,
                last_pdf_page=2,
                label="Climate statement",
            )
        )
        annual = uuid4()
        await c.execute(
            insert(Unit).values(
                id=annual,
                document_id=doc,
                kind="paragraph",
                seq=1,
                pdf_page=3,
                section_path="",
                text="climate risk",
                context_header="",
                parser_version="test",
            )
        )
        fid = await c.scalar(
            insert(Finding)
            .values(
                run_id=rid, check_id=check, status=status, rationale="test", steps=0, cost_usd=0
            )
            .returning(Finding.id)
        )
    return fid, llm.unit, annual


async def fact(
    env, fid, unit, value=1204, name="scope1_emissions", verified=True, role="supports", **kw
):
    async with env[0].begin() as c:
        await c.execute(
            insert(Evidence).values(
                finding_id=fid, unit_id=unit, quote="climate risk", role=role, verified=verified
            )
        )
        await c.execute(
            insert(Fact).values(
                finding_id=fid,
                unit_id=unit,
                name=name,
                value_num=value,
                unit=kw.get("unit_name", "tCO2e"),
                scope=kw.get("scope", "group"),
                year=kw.get("year", 2026),
                raw=kw.get("raw", str(value)),
            )
        )


async def compare(env):
    async with env[0].begin() as c:
        ids = await compare_run(c, env[1])
        rows = (
            (
                await c.execute(
                    select(Inconsistency)
                    .where(Inconsistency.run_id == env[1])
                    .order_by(Inconsistency.type)
                )
            )
            .mappings()
            .all()
        )
        f = (await c.execute(select(Finding).where(Finding.run_id == env[1]))).mappings().one()
        return ids, rows, f


@pytest.mark.parametrize(
    "options,expected",
    [
        ({}, "inconsistent"),
        ({"scope": "Australian operations"}, "needs_review"),
        ({"year": None}, "needs_review"),
        ({"year": 2025}, "needs_review"),
        ({"scope": ""}, "needs_review"),
        ({"unit_name": ""}, "needs_review"),
        ({"unit_name": "widgets"}, "needs_review"),
    ],
)
async def test_t3(run_env, options, expected):
    fid, a, b = await setup(run_env)
    await fact(run_env, fid, a)
    await fact(run_env, fid, b, 1.30, **({"unit_name": "kt"} | options))
    ids, rows, f = await compare(run_env)
    assert len(ids) == len(rows) == 1 and rows[0].type == "T3"
    assert f.status == expected and f.version == 2
    assert rows[0].detail["comparable"] == (expected == "inconsistent")
    assert rows[0].detail["side_a"]["part_kind"] != rows[0].detail["side_b"]["part_kind"]
    if expected == "needs_review":
        assert f.needs_review_reason == "possible_inconsistency"
    repeat_ids, repeat_rows, _ = await compare(run_env)
    assert repeat_ids == ids
    assert [r.detail for r in repeat_rows] == [r.detail for r in rows]
    async with run_env[0].connect() as c:
        assert (
            await c.scalar(
                select(func.count()).select_from(AuditLog).where(AuditLog.target == str(fid))
            )
            == 1
        )
    assert (await compare(run_env))[2].version == 2


@pytest.mark.parametrize("case", ["within", "same_kind", "unverified"])
async def test_t3_ignored(run_env, case):
    fid, a, b = await setup(run_env)
    await fact(run_env, fid, a)
    await fact(
        run_env,
        fid,
        a if case == "same_kind" else b,
        1.2 if case == "within" else 2,
        unit_name="kt",
        verified=case != "unverified",
    )
    assert not (await compare(run_env))[1]


@pytest.mark.parametrize("target", [False, True])
async def test_t4(run_env, target):
    fid, a, b = await setup(run_env, "M3")
    await fact(run_env, fid, b, name="safeguard_baseline")
    if target:
        await fact(run_env, fid, a, name="emissions_target")
    _, rows, f = await compare(run_env)
    assert len(rows) == (0 if target else 1)
    assert f.status == ("evidence_found" if target else "needs_review")
    if not target:
        assert f.needs_review_reason == "regulatory_target_missing"


@pytest.mark.parametrize(
    "raw,flag",
    [
        ("report section 4.2", False),
        ("unknown section 4.2", True),
        ("report", True),
        ("Climate statement page 4", False),
    ],
)
async def test_t5(run_env, raw, flag):
    fid, a, _ = await setup(run_env, "A3")
    await fact(run_env, fid, a, name="cross_reference_target", raw=raw)
    _, rows, f = await compare(run_env)
    assert len(rows) == int(flag)
    if flag:
        assert f.needs_review_reason == "cross_reference_unclear"


@pytest.mark.parametrize(
    "period,count", [("year ended 30 June 2025", 1), ("year ended 30 June 2026", 0), ("garbage", 0)]
)
async def test_t6(run_env, period, count):
    fid, a, b = await setup(run_env)
    await fact(run_env, fid, a, name="reporting_period", raw="FY2026")
    await fact(run_env, fid, b, name="reporting_period", raw=period)
    _, rows, f = await compare(run_env)
    assert len(rows) == count and f.status == "evidence_found"
    if rows:
        assert rows[0].type == "T6" and rows[0].finding_id is None


@pytest.mark.parametrize(
    "check,kind,status", [("A2", "T1", "inconsistent"), ("S4", "T2", "needs_review")]
)
async def test_worker_flags(run_env, check, kind, status):
    fid, a, b = await setup(run_env, check, "inconsistent")
    await fact(run_env, fid, a, role="side_a")
    await fact(run_env, fid, b, role="side_b")
    _, rows, f = await compare(run_env)
    assert rows[0].type == kind and f.status == status
    if kind == "T2":
        assert f.needs_review_reason == "t2_flag"
    assert len((await compare(run_env))[1]) == 1


@pytest.mark.parametrize(
    "check,status",
    [("M1", "error"), ("M1", "not_checked"), ("M3", "inconsistent"), ("A3", "inconsistent")],
)
async def test_precedence(run_env, check, status):
    fid, a, b = await setup(run_env, check, status)
    if check == "M1":
        await fact(run_env, fid, a)
        await fact(run_env, fid, b, 2, unit_name="kt")
    else:
        await fact(
            run_env,
            fid,
            b,
            name="safeguard_baseline" if check == "M3" else "cross_reference_target",
        )
    assert (await compare(run_env))[2].status == status


async def test_run_graph(run_env, run_helpers):
    import json
    from dataclasses import replace

    from cda.llm.types import ToolCall

    _, a, b = await setup(run_env)
    # The graph owns finding creation; retain only the document/part fixture.
    from sqlalchemy import delete

    async with run_env[0].begin() as c:
        await c.execute(delete(Finding).where(Finding.run_id == run_env[1]))
    original = run_env[3].complete

    async def scripted(**kw):
        response = await original(**kw)
        if "Check M1:" in kw["messages"][1].content:
            calls = []
            for call in response.message.tool_calls:
                if call.name == "submit_finding":
                    args = json.loads(call.arguments)
                    args["evidence"] = [
                        {"unit_id": str(u), "quote": "climate risk", "role": "supports"}
                        for u in (a, b)
                    ]
                    args["facts"] = [
                        {
                            "name": "scope1_emissions",
                            "value": v,
                            "unit": "tCO2e",
                            "scope": "group",
                            "year": 2026,
                            "unit_id": str(u),
                            "raw": str(v),
                        }
                        for u, v in [(a, 1204), (b, 1300)]
                    ]
                    call = ToolCall(call.id, call.name, json.dumps(args))
                calls.append(call)
            response = replace(response, message=replace(response.message, tool_calls=calls))
        return response

    run_env[3].complete = scripted
    run_env[2].comparer = compare_run
    assert await run_helpers.execute(run_env) == "final"
    fs = await run_helpers.findings(run_env)
    assert next(f for f in fs if f.check_id == "M1").status == "inconsistent"


@pytest.mark.parametrize(
    "kind,check,status,reason",
    [
        ("T3", "M1", "evidence_found", None),
        ("T4", "M3", "partial_evidence", "regulatory_target_missing"),
        ("T5", "A3", "no_evidence_found", "cross_reference_unclear"),
        ("T2", "S4", "inconsistent", "t2_flag"),
    ],
)
async def test_audit_and_version(run_env, kind, check, status, reason):
    fid, a, b = await setup(run_env, check, status)
    if kind == "T3":
        await fact(run_env, fid, a)
        await fact(run_env, fid, b, 1300)
    elif kind == "T4":
        await fact(run_env, fid, b, name="safeguard_baseline")
    elif kind == "T5":
        await fact(run_env, fid, a, name="cross_reference_target", raw="unknown")
    else:
        await fact(run_env, fid, a, role="side_a")
        await fact(run_env, fid, b, role="side_b")
    async with run_env[0].connect() as c:
        old_time = await c.scalar(select(Finding.updated_at).where(Finding.id == fid))
    _, _, f = await compare(run_env)
    assert f.version == 2 and f.updated_at > old_time
    assert f.needs_review_reason == reason
    async with run_env[0].connect() as c:
        audit = (
            (await c.execute(select(AuditLog).where(AuditLog.target == str(fid)))).mappings().one()
        )
        assert audit.actor == "system" and audit.action == "status_changed_by_compare"
        assert audit.detail == {
            "old_status": status,
            "new_status": f.status,
            "inconsistency_type": kind,
        }


@pytest.mark.parametrize("case", ["absent", "other_finding", "duplicate"])
async def test_verified_evidence_is_finding_specific(run_env, case):
    from sqlalchemy import delete

    fid, a, b = await setup(run_env)
    await fact(run_env, fid, a)
    await fact(run_env, fid, b, 1300)
    async with run_env[0].begin() as c:
        if case != "duplicate":
            await c.execute(
                delete(Evidence).where(Evidence.finding_id == fid, Evidence.unit_id == b)
            )
            if case == "other_finding":
                other = await c.scalar(
                    insert(Finding)
                    .values(
                        run_id=run_env[1],
                        check_id="M2",
                        status="evidence_found",
                        rationale="test",
                        steps=0,
                        cost_usd=0,
                    )
                    .returning(Finding.id)
                )
                await c.execute(
                    insert(Evidence).values(
                        finding_id=other,
                        unit_id=b,
                        quote="climate risk",
                        role="supports",
                        verified=True,
                    )
                )
        else:
            await c.execute(
                insert(Evidence).values(
                    finding_id=fid, unit_id=b, quote="climate risk", role="supports", verified=True
                )
            )
        ids = await compare_run(c, run_env[1])
        assert len(ids) == int(case == "duplicate")


@pytest.mark.parametrize(
    "raw,document_kind,part_kind,filename,label,flag",
    [
        ("refer to the Annual Report, Note 23", "annual", "other", "report.pdf", None, False),
        ("refer to the Annual Report, Note 23", "other", "annual", "report.pdf", None, False),
        (
            "refer to the Annual Report, Note 23",
            "other",
            "sustainability",
            "wattlebrook-pack.pdf",  # a stem like "report" would match any text
            None,
            True,
        ),
        (
            "see the 2026 Climate Report, page 12",
            "other",
            "sustainability",
            "wattlebrook-pack.pdf",  # a stem like "report" would match any text
            None,
            False,
        ),
        (
            "cba 2026 annual report section 4",
            "other",
            "other",
            "cba-2026-annual-report.pdf",
            None,
            False,
        ),
        (
            "cba 2026 annual report section 4",
            "other",
            "other",
            "cba_2026_annual_report.pdf",
            None,
            False,
        ),
        ("climate appendix page 12", "other", "other", "report.pdf", "Climate_Appendix", False),
        (
            "CBA-2026_ANNUAL  REPORT, Note 23",
            "other",
            "other",
            "cba-2026-annual-report.pdf",
            None,
            False,
        ),
        ("Annual Report", "annual", "other", "report.pdf", None, True),
    ],
)
async def test_t5_document_names(run_env, raw, document_kind, part_kind, filename, label, flag):
    fid, a, _ = await setup(run_env, "A3")
    async with run_env[0].begin() as c:
        doc = await c.scalar(select(Unit.document_id).where(Unit.id == a))
        await c.execute(
            update(Document).where(Document.id == doc).values(kind=document_kind, filename=filename)
        )
        await c.execute(
            update(DocumentPart)
            .where(DocumentPart.document_id == doc)
            .values(kind=part_kind, label=label)
        )
    await fact(run_env, fid, a, name="cross_reference_target", raw=raw)
    _, rows, finding = await compare(run_env)
    assert len(rows) == int(flag)
    assert finding.status == ("needs_review" if flag else "evidence_found")
    if flag:
        assert rows[0].type == "T5"
        assert finding.needs_review_reason == "cross_reference_unclear"


@pytest.mark.parametrize(
    "name",
    [
        "sustainability report",
        "climate report",
        "climate-related financial disclosure",
        "climate-related financial disclosures",
        "climate statement",
    ],
)
async def test_t5_sustainability_names(run_env, name):
    fid, a, _ = await setup(run_env, "A3")
    await fact(run_env, fid, a, name="cross_reference_target", raw=f"See {name}, page 12")
    assert not (await compare(run_env))[1]
