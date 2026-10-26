import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from cda.api import deps
from cda.api.app import create_app
from cda.api.export import csv_cell, markdown
from cda.api.routes.findings import ReviewInput
from cda.api.routes.runs import RunInput
from cda.indexing.embed import HashEmbedder


@pytest.mark.parametrize("value", ["=SUM(A1:A2)", "+x", "-x", "@x", "\tvalue", "\rvalue"])
def test_formula_prefixes(value):
    assert csv_cell(value) == "'" + value


def test_markdown_all_specials():
    chars = "\\`*_{}[]()#+-.!|<>"
    assert markdown(chars) == "".join("\\" + char for char in chars)
    assert markdown("* | [x](y) <script>") == r"\* \| \[x\]\(y\) \<script\>"


@pytest.mark.parametrize(
    "body",
    [
        {"decision": "accept", "version": 1},
        {"decision": "accept", "version": 1, "note": ""},
        {"decision": "accept", "version": 1, "note": "x", "new_status": "inconsistent"},
        {"decision": "change", "version": 1, "note": "x"},
    ],
)
def test_invalid_reviews(body):
    with pytest.raises(ValidationError):
        ReviewInput.model_validate(body)


@pytest.mark.parametrize("field,value", [("method", "LC"), ("budget_usd", 0), ("budget_usd", 11)])
def test_invalid_run_body(field, value):
    from uuid import uuid4

    body = {"pack_id": uuid4(), "method": "A", "model_key": "luna"}
    body[field] = value
    with pytest.raises(ValidationError):
        RunInput.model_validate(body)


async def test_safe_500_without_database():
    app = create_app(embedder=HashEmbedder())

    async def actor():
        return {"handle": "sarah"}

    async def failing_query():
        raise RuntimeError("SELECT private FROM /private/file")

    app.dependency_overrides[deps.identity] = actor
    app.dependency_overrides[deps.connection] = failing_query
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.get("/packs")
    assert response.status_code == 500
    assert response.json() == {"error": "internal_error", "message": "Request failed"}
