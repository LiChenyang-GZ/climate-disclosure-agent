"""Build Wattlebrook's deterministic disclosure pack: python -m cda.synth.wattlebrook."""

import argparse
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape

import yaml
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph, Table, TableStyle

from cda.synth.answer_key import (
    AnswerKey,
    Cell,
    CheckId,
    Difficulty,
    Document,
    Evidence,
    Issue,
    PartKind,
    Scan,
)
from cda.synth.content import Page, annual_pages, supplement_pages

ANNUAL = "wattlebrook-annual-report-2026.pdf"
SUPPLEMENT = "wattlebrook-climate-data-supplement-2026.pdf"


@dataclass(frozen=True)
class Location:
    document: str
    pdf_page: int
    column: Literal["left", "right", "single"]
    printed_page: int | None
    heading: str
    quote: str
    source_type: Literal["text", "table"] = "text"


@dataclass
class PackManifest:
    locations: dict[str, Location] = field(default_factory=dict)
    page_counts: dict[str, int] = field(default_factory=dict)


BODY = ParagraphStyle(
    "body", fontName="Helvetica", fontSize=8.5, leading=10.5, textColor=colors.HexColor("#243746")
)
CELL = ParagraphStyle("cell", fontName="Helvetica", fontSize=7.5, leading=9)


def _render(pages: list[Page], name: str, manifest: PackManifest) -> bytes:
    stream = BytesIO()
    canvas = Canvas(stream, pagesize=A4, invariant=True, pageCompression=1)
    canvas.setTitle(
        "Wattlebrook Energy Limited | FY2026 | "
        + ("Annual Report" if name == ANNUAL else "Climate Data Supplement")
    )
    canvas.setAuthor("Wattlebrook Energy Limited (fictional)")
    canvas.setSubject("Deterministic fictional climate disclosure research fixture")
    canvas.setCreator("Climate Disclosure Agent synthetic pack generator")
    for number, page in enumerate(pages, 1):
        spread = page.tag == "spread"
        width, height = landscape(A4) if spread else A4
        canvas.setPageSize((width, height))
        printed = number if name == SUPPLEMENT else number - 1 if number >= 4 else None

        def record(
            tag: str,
            text: str,
            column: Literal["left", "right", "single"] = "single",
            table: bool = False,
            *,
            page_number: int = number,
            printed_number: int | None = printed,
            section_heading: str = page.heading,
        ) -> None:
            manifest.locations[tag] = Location(
                name,
                page_number,
                column,
                printed_number,
                section_heading,
                text,
                "table" if table else "text",
            )

        canvas.setFillColor(colors.HexColor("#0b5351"))
        if page.tag != "cover" and not spread:
            canvas.setFont("Helvetica", 9)
            header = (
                "Wattlebrook Energy Limited | Annual Report 2026"
                if name == ANNUAL
                else "Wattlebrook Climate Data Supplement 2026"
            )
            canvas.drawString(42, height - 30, header)
            canvas.setStrokeColor(colors.HexColor("#8cbab5"))
            canvas.line(42, height - 44, width - 42, height - 44)
        if printed is not None:
            canvas.setFont("Helvetica", 9)
            canvas.drawCentredString(width / 2, 28, str(printed))
        if spread:
            for i, (tag, text) in enumerate(page.paragraphs):
                x = 42 + i * width / 2
                canvas.setFont("Helvetica-Bold", 18)
                canvas.drawString(
                    x, height - 100, "Our year at a glance" if i == 0 else "Serving our customers"
                )
                paragraph = Paragraph(escape(text), BODY)
                _, h = paragraph.wrap(width / 2 - 84, 300)
                paragraph.drawOn(canvas, x, height - 150 - h)
                canvas.setFont("Helvetica", 9)
                canvas.drawString(x, 28, str(i + 1))
                record(tag, text, "left" if i == 0 else "right")
            canvas.showPage()
            continue
        heading = Paragraph(
            escape(page.heading),
            ParagraphStyle(
                "heading",
                fontName="Helvetica-Bold",
                fontSize=21,
                leading=25,
                textColor=colors.HexColor("#0b5351"),
            ),
        )
        _, h = heading.wrap((width - 112) / 2 if page.columns else width - 84, 150)
        top = height - 92
        heading.drawOn(canvas, 42, top - h)
        record(page.tag, page.heading)
        top -= h + 28
        if page.table:
            tag, headers, rows = page.table
            data = [[Paragraph(escape(cell), CELL) for cell in row] for row in [headers, *rows]]
            table = Table(data, colWidths=[(width - 84) / len(headers)] * len(headers))
            table.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#567875")),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5efed")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            _, table_height = table.wrap(width - 84, height)
            table.drawOn(canvas, 42, top - table_height)
            top -= table_height + 26
            record(tag, rows[0][0], table=True)
            for i, row in enumerate(rows):
                record(f"{tag}_row{i}", row[0], table=True)
        # Fixed frames ensure one paragraph can never cross a column or page.
        column_width = (width - 112) / 2 if page.columns else width - 84
        split = (len(page.paragraphs) + 1) // 2
        ys = [top, top - 8]
        for i, (tag, text) in enumerate(page.paragraphs):
            col = int(page.columns and i >= split)
            if page.columns and i == split:
                # A vertical gutter also prevents short cross-column paragraphs
                # being coalesced into one PyMuPDF text block.
                ys[1] = top - 8
            x = 42 + col * (column_width + 28)
            style = (
                ParagraphStyle("subheading", fontName="Helvetica-Bold", fontSize=14, leading=18)
                if tag.endswith("_heading")
                else BODY
            )
            paragraph = Paragraph(escape(text), style)
            _, para_height = paragraph.wrap(column_width, height)
            if ys[col] - para_height < 80:
                raise ValueError(f"content exceeds frame: {tag}")
            paragraph.drawOn(canvas, x, ys[col] - para_height)
            ys[col] -= para_height + 12
            record(
                tag,
                page.quotes.get(tag, text),
                ("right" if col else "left") if page.columns else "single",
            )
            for source, index in page.source_paragraphs.items():
                if index == i:
                    record(
                        source,
                        page.quotes[source],
                        ("right" if col else "left") if page.columns else "single",
                    )
        canvas.showPage()
    canvas.save()
    manifest.page_counts[name] = len(pages)
    return stream.getvalue()


def _answer_key(manifest: PackManifest) -> AnswerKey:
    first = manifest.locations["sr_start"].pdf_page
    last = manifest.locations["index"].pdf_page

    def evidence(
        tag: str,
        difficulty: Difficulty = "paraphrase",
        quote: str | None = None,
        alternate: bool = False,
    ) -> Evidence:
        loc = manifest.locations[tag]
        part: PartKind = (
            "sustainability"
            if loc.document == ANNUAL and first <= loc.pdf_page <= last
            else "annual"
            if loc.document == ANNUAL
            else "other"
        )
        return Evidence(
            document=loc.document,
            part=part,
            pdf_page=loc.pdf_page,
            quote=quote or loc.quote,
            role="alternate" if alternate else "primary",
            source_type=loc.source_type,
            difficulty=difficulty,
        )

    scans = [
        Scan(document=name, first_pdf_page=1, last_pdf_page=count)
        for name, count in manifest.page_counts.items()
    ]
    cells: list[Cell] = []

    def cell(
        check: CheckId,
        status: Literal["found", "partial", "not_found", "inconsistent"],
        difficulty: Difficulty | None,
        tags: list[str],
        relief: Literal[
            "scope3_first_year", "no_prior_period_progress", "not_separately_identifiable"
        ]
        | None = None,
    ) -> None:
        cells.append(
            Cell(
                check_id=check,
                status=status,
                difficulty=difficulty,
                relief_applied=relief,
                evidence=[
                    evidence(
                        t,
                        "direct"
                        if difficulty == "direct"
                        else "table"
                        if manifest.locations[t].source_type == "table"
                        else "paraphrase",
                    )
                    for t in tags
                ],
                scanned=scans if status in ("not_found", "inconsistent") else [],
            )
        )

    cell("G1", "found", "split", ["g1", "skills_table_row0"])
    cell("G2", "not_found", None, [])
    cell("S1", "found", "table", [*(f"risk_table_row{i}" for i in range(7)), "horizons"])
    cell("S2", "found", "paraphrase", ["s2"])
    cell("S3", "partial", "paraphrase", ["s3", "s3_relief"], "no_prior_period_progress")
    cell(
        "S4",
        "found",
        "paraphrase",
        ["s4", "s4_relief", "s4_qualitative"],
        "not_separately_identifiable",
    )
    cell("S5", "partial", "direct", ["s5", "s5_result", "s5_future"])
    cell("R1", "found", "split", ["r1_sr", "r1_ar"])
    cell("M1", "inconsistent", "split", ["m1_sr", "m1_relief", "scope_ar"], "scope3_first_year")
    cells[-1].evidence += [
        evidence(f"emissions_table_row{i}", "table", q)
        for i, q in enumerate(["Scope 1 gross", "Scope 2 location-based", "Scope 2 market-based"])
    ]
    cell("M2", "found", "split", ["m2_capital", "m2_price", "scorecard_row0", "asset_table_row0"])
    # The mandated carbon-price sentence deliberately uses the check's wording.
    next(
        e for e in cells[-1].evidence if e.quote == manifest.locations["m2_price"].quote
    ).difficulty = "direct"
    cell("M3", "inconsistent", "split", ["m3", "m3_progress", "safeguard"])
    cell("A1", "partial", "direct", ["disclaimer"])
    cell("A2", "inconsistent", "split", ["flood", *(f"risk_table_row{i}" for i in range(4))])
    cells[-1].evidence.append(evidence("flood_alternate", alternate=True))
    cell("A3", "inconsistent", "direct", ["good_reference", "bad_web", "bad_third"])
    cell("A4", "found", "table", [f"index_table_row{i}" for i in range(5)])
    issues = [
        Issue(
            id="t3_scope1",
            type="T3",
            check_id="M1",
            locations=[
                evidence("scope_ar"),
                evidence("m1_sr"),
                evidence("emissions_table_row0", "table", "Scope 1 gross"),
            ],
        ),
        Issue(
            id="t4_regulatory_target",
            type="T4",
            check_id="M3",
            locations=[evidence("safeguard"), evidence("m3")],
        ),
        Issue(
            id="t1_flood",
            type="T1",
            check_id="A2",
            locations=[
                evidence("flood"),
                *(evidence(f"risk_table_row{i}", "table") for i in range(4)),
            ],
        ),
        Issue(id="t5_website", type="T5", check_id="A3", locations=[evidence("bad_web")]),
        Issue(id="t5_third_party", type="T5", check_id="A3", locations=[evidence("bad_third")]),
        Issue(
            id="disclaimer", type="disclaimer", check_id="A1", locations=[evidence("disclaimer")]
        ),
        Issue(
            id="single_scenario", type="single_scenario", check_id="S5", locations=[evidence("s5")]
        ),
        Issue(id="injection_methodology", type="injection", locations=[evidence("injection_sup")]),
        Issue(id="injection_footnote", type="injection", locations=[evidence("injection_sr")]),
    ]
    # Reuse the cell entry, including its source, role and difficulty labels.
    shared = {(e.document, e.pdf_page, e.quote): e for c in cells for e in c.evidence}
    for issue in issues:
        issue.locations = [
            shared.get((e.document, e.pdf_page, e.quote), e) for e in issue.locations
        ]
    return AnswerKey(
        documents=[
            Document(filename=ANNUAL, kind="annual"),
            Document(filename=SUPPLEMENT, kind="other"),
        ],
        cells=cells,
        planted_issues=issues,
    )


def build_pack(
    out_dir: Path, *, s5_position: Literal["start", "middle", "end"] = "middle"
) -> PackManifest:
    """Write two PDFs and computed YAML; return the tagged build locations."""
    if s5_position not in ("start", "middle", "end"):
        raise ValueError("s5_position must be start, middle or end")
    pages = annual_pages(s5_position)
    first_pass = PackManifest()
    _render(pages, ANNUAL, first_pass)
    index = next(p for p in pages if p.tag == "index")
    index.table = (
        "index_table",
        ["Requirement", "Area", "Printed page"],
        [
            [
                f"AASB S2 {'para' if requirement in ('6', '29') else 'paras'} {requirement} ({area})",
                first_pass.locations[tag].heading,
                str(first_pass.locations[tag].printed_page),
            ]
            for tag, requirement, area in [
                ("governance", "6", "Governance"),
                ("strategy", "10-22", "Strategy"),
                ("risk", "24-26", "Risk management"),
                ("metrics", "29", "Metrics"),
                ("targets", "33-36", "Targets"),
            ]
        ],
    )
    contents = next(p for p in pages if p.tag == "contents")
    contents.paragraphs += [
        (
            "contents_" + tag,
            f"{first_pass.locations[tag].heading}: printed page {first_pass.locations[tag].printed_page}.",
        )
        for tag in ("letter", "ofr", "directors", "sr_start", "financial", "shareholders")
    ]
    manifest = PackManifest()
    annual = _render(pages, ANNUAL, manifest)
    for tag, loc in first_pass.locations.items():
        if manifest.locations[tag].pdf_page != loc.pdf_page:
            raise ValueError("second pass moved a page")
    supplement = _render(supplement_pages(), SUPPLEMENT, manifest)
    key = _answer_key(manifest)
    parts = {
        "documents": [
            {
                "filename": ANNUAL,
                "kind": "annual",
                "parts": [
                    {
                        "kind": "sustainability",
                        "first_pdf_page": manifest.locations["sr_start"].pdf_page,
                        "last_pdf_page": manifest.locations["index"].pdf_page,
                        "label": "Climate-related financial disclosures",
                    }
                ],
            },
            {"filename": SUPPLEMENT, "kind": "other", "parts": []},
        ]
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ANNUAL).write_bytes(annual)
    (out_dir / SUPPLEMENT).write_bytes(supplement)
    for name, data in [("parts.yaml", parts), ("answer_key.yaml", key.model_dump(mode="json"))]:
        (out_dir / name).write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000),
            encoding="utf-8",
            newline="\n",
        )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--s5-position", choices=("start", "middle", "end"), default="middle")
    args = parser.parse_args()
    build_pack(args.out, s5_position=args.s5_position)


if __name__ == "__main__":
    main()
