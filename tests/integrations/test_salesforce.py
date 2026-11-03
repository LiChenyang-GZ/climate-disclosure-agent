"""Salesforce REST client against recorded-shape responses (no network)."""

import json
import logging
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from cda.integrations.salesforce import (
    API_VERSION,
    Credentials,
    SalesforceClient,
    SalesforceConnection,
    SalesforceLimit,
    SalesforceRejected,
    SalesforceRequestFailed,
    SalesforceUnavailable,
    account_search_soql,
    case_description,
    case_fields,
    case_subject,
    escape_like,
    normalize_instance_url,
)

FIXTURES = Path(__file__).parent / "fixtures" / "salesforce"
INSTANCE = "https://wattlebrook-dev-ed.develop.my.salesforce.com"
SECRET = "3A1F-client-secret-never-shown"
TOKEN = "00D5j00000FakeAAA!AQ4AQFakeAccessTokenOne"
DATA = f"/services/data/{API_VERSION}"


def fixture(name: str) -> object:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def creds(secret: str = SECRET) -> Credentials:
    return Credentials(INSTANCE, "3MVG9-consumer-key", SecretStr(secret))


class Recorder:
    """Answers by path from per-path queues; the last answer repeats."""

    def __init__(self, routes: dict[str, list[tuple[int, object]]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.routes[request.url.path]
        status, body = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(status, json=body)

    def count(self, path: str) -> int:
        return sum(1 for r in self.requests if r.url.path == path)


def client(recorder: Recorder, secret: str = SECRET) -> SalesforceClient:
    return SalesforceClient(creds(secret), transport=httpx.MockTransport(recorder))


TOKEN_OK = {"/services/oauth2/token": [(200, fixture("token"))]}


async def test_connect_reads_edition_with_client_credentials() -> None:
    rec = Recorder({**TOKEN_OK, f"{DATA}/query": [(200, fixture("organization"))]})
    assert await client(rec).edition() == "Developer Edition"
    token_request = rec.requests[0]
    assert str(token_request.url) == f"{INSTANCE}/services/oauth2/token"
    assert token_request.method == "POST"
    form = parse_qs(token_request.content.decode())
    assert form == {
        "grant_type": ["client_credentials"],
        "client_id": ["3MVG9-consumer-key"],
        "client_secret": [SECRET],
    }
    # The secret travels only in the POST body, never in a URL.
    assert all(SECRET not in str(r.url) for r in rec.requests)
    query = rec.requests[1]
    assert query.headers["authorization"] == f"Bearer {TOKEN}"
    assert "Organization" in query.url.params["q"]


async def test_invalid_client_is_rejected() -> None:
    rec = Recorder({"/services/oauth2/token": [(400, fixture("token_invalid_client"))]})
    with pytest.raises(SalesforceRejected):
        await client(rec).edition()


async def test_connection_keeps_credentials_only_after_success() -> None:
    bad = Recorder({"/services/oauth2/token": [(400, fixture("token_invalid_client"))]})
    connection = SalesforceConnection(None, transport=httpx.MockTransport(bad))
    with pytest.raises(SalesforceRejected):
        await connection.connect(creds())
    assert not connection.configured
    good = Recorder({**TOKEN_OK, f"{DATA}/query": [(200, fixture("organization"))]})
    connection = SalesforceConnection(None, transport=httpx.MockTransport(good))
    assert await connection.connect(creds()) == "Developer Edition"
    assert connection.status() == {
        "configured": True,
        "instance_url": INSTANCE,
        "edition": "Developer Edition",
    }


async def test_secrets_never_in_logs_reprs_or_errors(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    rec = Recorder({**TOKEN_OK, f"{DATA}/query": [(200, fixture("organization"))]})
    connection = SalesforceConnection(None, transport=httpx.MockTransport(rec))
    await connection.connect(creds())
    bad = Recorder({"/services/oauth2/token": [(400, fixture("token_invalid_client"))]})
    with pytest.raises(SalesforceRejected) as rejected:
        await client(bad).edition()
    texts = [caplog.text, repr(creds()), str(creds()), repr(connection), str(rejected.value)]
    texts.append(repr(connection.status()))
    for text in texts:
        assert SECRET not in text
        assert TOKEN not in text


def test_escape_like_escapes_quotes_backslashes_and_wildcards() -> None:
    assert escape_like("O'Brien 50%_x\\y\"z") == "O\\'Brien 50\\%\\_x\\\\y\\\"z"
    assert escape_like("a\nb") == "a\\nb"
    assert (
        account_search_soql("it's 100%")
        == "SELECT Id, Name FROM Account WHERE Name LIKE '%it\\'s 100\\%%' LIMIT 10"
    )


async def test_account_search_sends_escaped_term() -> None:
    rec = Recorder({**TOKEN_OK, f"{DATA}/query": [(200, fixture("accounts"))]})
    accounts = await client(rec).search_accounts("Watt' OR Name LIKE '%")
    assert accounts == [
        {"id": "0015j00000WattAAAQ", "name": "Wattlebrook Energy Ltd"},
        {"id": "0015j00000WatsAAAQ", "name": "Wattlebrook Holdings"},
    ]
    soql = rec.requests[-1].url.params["q"]
    assert soql == (
        "SELECT Id, Name FROM Account WHERE Name LIKE '%Watt\\' OR Name LIKE \\'\\%%' LIMIT 10"
    )


async def test_account_search_no_results() -> None:
    rec = Recorder({**TOKEN_OK, f"{DATA}/query": [(200, fixture("accounts_empty"))]})
    assert await client(rec).search_accounts("Nobody") == []


async def test_expired_token_is_refreshed_once() -> None:
    rec = Recorder(
        {
            "/services/oauth2/token": [(200, fixture("token")), (200, fixture("token_refreshed"))],
            f"{DATA}/sobjects/Case": [
                (401, fixture("error_invalid_session")),
                (201, fixture("case_created")),
            ],
        }
    )
    sf = client(rec)
    assert await sf.create_case({"Subject": "x"}) == "5005j00000Case1AAA"
    assert rec.count("/services/oauth2/token") == 2
    last = rec.requests[-1]
    assert last.headers["authorization"].endswith("FakeAccessTokenTwo")


async def test_still_unauthorised_after_refresh_is_rejected() -> None:
    rec = Recorder({**TOKEN_OK, f"{DATA}/sobjects/Case": [(401, fixture("error_invalid_session"))]})
    with pytest.raises(SalesforceRejected):
        await client(rec).create_case({"Subject": "x"})
    assert rec.count("/services/oauth2/token") == 2
    assert rec.count(f"{DATA}/sobjects/Case") == 2


@pytest.mark.parametrize(
    "status,body", [(403, fixture("error_request_limit")), (429, [{"errorCode": "x"}])]
)
async def test_limits(status: int, body: object) -> None:
    rec = Recorder({**TOKEN_OK, f"{DATA}/sobjects/Case": [(status, body)]})
    with pytest.raises(SalesforceLimit):
        await client(rec).create_case({"Subject": "x"})
    assert rec.count(f"{DATA}/sobjects/Case") == 1


async def test_case_rejected_by_validation_is_a_request_failure() -> None:
    rec = Recorder(
        {**TOKEN_OK, f"{DATA}/sobjects/Case": [(400, fixture("error_restricted_picklist"))]}
    )
    with pytest.raises(SalesforceRequestFailed) as failed:
        await client(rec).create_case({"Subject": "x"})
    assert failed.value.error_code == "INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST"
    assert "restricted picklist" in failed.value.message


async def test_server_errors_and_network_failures_are_unavailable() -> None:
    rec = Recorder({**TOKEN_OK, f"{DATA}/query": [(503, {"message": "down"})]})
    with pytest.raises(SalesforceUnavailable):
        await client(rec).search_accounts("x")

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    with pytest.raises(SalesforceUnavailable):
        await SalesforceClient(creds(), transport=httpx.MockTransport(boom)).edition()


async def test_case_number_lookup() -> None:
    rec = Recorder(
        {**TOKEN_OK, f"{DATA}/sobjects/Case/5005j00000Case1AAA": [(200, fixture("case_number"))]}
    )
    assert await client(rec).case_number("5005j00000Case1AAA") == "00001026"
    assert rec.requests[-1].url.params["fields"] == "CaseNumber"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.my.salesforce.com",
        "https://user:pw@example.my.salesforce.com",
        "https://example.my.salesforce.com/services",
        "https://example.my.salesforce.com?x=1",
        "example.my.salesforce.com",
        "",
    ],
)
def test_instance_url_must_be_a_plain_https_origin(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_instance_url(url)


def test_instance_url_normalised() -> None:
    assert normalize_instance_url(" https://Example.my.salesforce.com/ ") == (
        "https://example.my.salesforce.com"
    )


def test_case_subject_is_capped() -> None:
    assert case_subject("G1", "Board oversight") == "G1 · Board oversight"
    long = case_subject("G1", "x" * 400)
    assert len(long) == 255
    assert long.endswith("…")


def test_case_description_and_fields() -> None:
    evidence = [
        {
            "filename": "annual.pdf",
            "pdf_page": 12,
            "printed_page": "10",
            "part_label": "Climate report",
            "quote": 'The Board "oversees" climate risk.',
        },
        {"filename": "data.xlsx", "pdf_page": 2, "printed_page": None, "quote": "Scope 1: 10"},
    ]
    text = case_description(
        status="partial_evidence",
        rationale="Board oversight is described but frequency is missing.",
        missing_aspects=["meeting frequency"],
        evidence=evidence,
        report_link="/runs/r1/report",
    )
    assert "Status: Partial evidence" in text
    assert "Board oversight is described but frequency is missing." in text
    assert "meeting frequency" in text
    assert "annual.pdf, Climate report, page 12 (printed page 10)" in text
    assert 'Quote: "The Board "oversees" climate risk."' in text
    assert "data.xlsx, sheet 2" in text
    assert "Report: /runs/r1/report" in text
    assert "<" not in text  # plain text
    none = case_description(
        status="no_evidence_found",
        rationale="Nothing found.",
        missing_aspects=[],
        evidence=[],
        report_link="/runs/r1/report",
    )
    assert "No evidence was cited." in none
    fields = case_fields("G1", "Board oversight", "0015j00000WattAAAQ", text)
    assert fields == {
        "Subject": "G1 · Board oversight",
        "Description": text,
        "AccountId": "0015j00000WattAAAQ",
        "Origin": "Climate Disclosure Agent",
    }
    huge = case_description(
        status="inconsistent",
        rationale="r" * 40_000,
        missing_aspects=[],
        evidence=[],
        report_link="/runs/r1/report",
    )
    assert len(huge) <= 32_000
    assert huge.endswith("Report: /runs/r1/report")
