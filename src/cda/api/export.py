"""Plain report data with context-specific export escaping."""

import csv
import io
import json
import re
from datetime import date
from typing import Any, Literal

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response

from cda.api import deps
from cda.api.deps import Actor, Conn
from cda.api.routes.findings import finding_detail, finding_rows
from cda.llm.cost import ModelTable

_ESCAPE = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>])")


def markdown(value: object) -> str:
    return (
        _ESCAPE.sub(r"\\\1", str(value)).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    )


def csv_cell(value: object) -> str:
    value = str(value)
    return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value


async def export_response(
    conn: Conn, actor: Actor, run: dict[str, Any], format: Literal["md", "json", "csv"]
) -> Response:
    pack = await deps.visible_pack(conn, actor, run["pack_id"])
    header = {
        key: run[key] for key in ("method", "model_key", "prompt_sha256", "check_set_version")
    }
    header.update(
        company=pack["company"],
        reporting_year=pack["reporting_year"],
        status="Final" if run["status"] == "final" else "Draft",
    )
    rows = await finding_rows(conn, actor, run["id"])
    review_cells = {}
    for row in rows:
        if row["id"]:
            detail = await finding_detail(conn, actor, row["id"])
            row["evidence"] = [
                {"document": e["filename"], "pdf_page": e["pdf_page"], "quote": e["quote"]}
                for e in detail["evidence"]
            ]
            review_cells[row["check_id"]] = "; ".join(
                f"{'accepted' if r['decision'] == 'accept' else 'changed'}: {markdown(r['note'])}"
                for r in detail["reviews"]
            )
            row["review_note"] = "\n".join(r["note"] for r in detail["reviews"])
        else:
            row["evidence"], row["review_note"] = [], ""
    data = jsonable_encoder({"header": header, "findings": rows})
    if format == "json":
        return JSONResponse(data)
    if format == "md":
        model = ModelTable.load()[run["model_key"]].model
        content = (
            f"# Climate disclosure evidence report \u2014 {markdown(pack['company'])} "
            f"{pack['reporting_year']}\n"
            f"**Status:** {header['status']} \u00b7 **Method:** {run['method']} \u00b7 "
            f"**Model:** {run['model_key']} ({model}) \u00b7 "
            f"**Check set:** {run['check_set_version']} \u00b7 "
            f"**Prompt:** {run['prompt_sha256'][:12]} \u00b7 **Exported:** {date.today().isoformat()}\n\n"
            "| Check | Title | Status | Relief | Evidence | Review |\n"
            "|---|---|---|---|---|---|\n"
        )
        for row in rows:
            evidence = "; ".join(
                f'{markdown(e["document"])} p.{e["pdf_page"]}: "{markdown(e["quote"])}"'
                for e in row["evidence"]
            )
            cells = [
                markdown(row["check_id"]),
                markdown(row["title"]) if row["title"] else "\u2014",
                row["status"].replace("_", " ").capitalize(),
                "Relief applied" if row["relief_applied"] else "\u2014",
                evidence or "\u2014",
                review_cells.get(row["check_id"]) or "\u2014",
            ]
            content += "| " + " | ".join(cells) + " |\n"
        return Response(
            content,
            media_type="text/markdown",
            headers={"Content-Disposition": 'attachment; filename="report.md"'},
        )
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["field", "value"])
    for key, value in header.items():
        writer.writerow([csv_cell(key), csv_cell(value)])
    keys = ["check_id", "title", "status", "relief_applied", "evidence", "review_note"]
    writer.writerow(keys)
    for row in rows:
        writer.writerow(
            [
                csv_cell(
                    json.dumps(row[key], ensure_ascii=False) if key == "evidence" else row[key]
                )
                for key in keys
            ]
        )
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="report.csv"'},
    )
