"""Salesforce settings, pack link and Case push through the API (D85), faked Salesforce."""

import json
import logging
from pathlib import Path
from urllib.parse import parse_qs
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import insert, select
from test_api import pack, run

from cda.db.models import AuditLog, Evidence, Finding, SalesforceCase, Unit
from cda.integrations.salesforce import API_VERSION, Credentials, SalesforceConnection

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).parents[1] / "integrations" / "fixtures" / "salesforce"
INSTANCE = "https://wattlebrook-dev-ed.develop.my.salesforce.com"
SECRET = "3A1F-client-secret-never-shown"
DATA = f"/services/data/{API_VERSION}"
ACCOUNT = {"id": "0015j00000WattAAAQ", "name": "Wattlebrook Energy Ltd"}
GOOD = {"instance_url": INSTANCE, "client_id": "3MVG9-consumer-key", "client_secret": SECRET}


def fixture(name: str) -> object:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


TOKENS = [fixture("token")["access_token"], fixture("token_refreshed")["access_token"]]


class FakeSalesforce:
    """A tiny Salesforce org: token endpoint, SOQL, Case create and Case read."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.token_status = 200
        self.fail_checks: set[str] = set()
        self.limit_after: int | None = None
        self.unauthorised = 0  # next N API calls answer 401
        self.created: list[dict[str, str]] = []
        self.tokens = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/services/oauth2/token":
            if self.token_status != 200:
                return httpx.Response(self.token_status, json=fixture("token_invalid_client"))
            self.tokens += 1
            return httpx.Response(
                200, json=fixture("token" if self.tokens == 1 else "token_refreshed")
            )
        if self.unauthorised:
            self.unauthorised -= 1
            return httpx.Response(401, json=fixture("error_invalid_session"))
        if path == f"{DATA}/query":
            soql = request.url.params["q"]
            if "FROM Organization" in soql:
                return httpx.Response(200, json=fixture("organization"))
            if "Nobody" in soql:
                return httpx.Response(200, json=fixture("accounts_empty"))
            return httpx.Response(200, json=fixture("accounts"))
        if path == f"{DATA}/sobjects/Case" and request.method == "POST":
            body = json.loads(request.content)
            if self.limit_after is not None and len(self.created) >= self.limit_after:
                return httpx.Response(403, json=fixture("error_request_limit"))
            if body["Subject"].split(" ")[0] in self.fail_checks:
                return httpx.Response(400, json=fixture("error_restricted_picklist"))
            case_id = f"5005j00000Case{len(self.created) + 1:03d}A"
            self.created.append({**body, "Id": case_id})
            return httpx.Response(201, json={"id": case_id, "success": True, "errors": []})
        if path.startswith(f"{DATA}/sobjects/Case/") and request.method == "GET":
            case_id = path.rsplit("/", 1)[1]
            n = next(i for i, c in enumerate(self.created, 1) if c["Id"] == case_id)
            return httpx.Response(
                200, json={"attributes": {"type": "Case"}, "Id": case_id, "CaseNumber": f"{n:08d}"}
            )
        return httpx.Response(404, json=[{"errorCode": "NOT_FOUND", "message": path}])

    @property
    def creates(self) -> int:
        return sum(
            1 for r in self.requests if r.url.path == f"{DATA}/sobjects/Case" and r.method == "POST"
        )

    def count(self, path: str) -> int:
        return sum(1 for r in self.requests if r.url.path == path)


def install(api, fake: FakeSalesforce, *, configured: bool = True) -> None:
    creds = Credentials(INSTANCE, "3MVG9-consumer-key", SecretStr(SECRET)) if configured else None
    api[2].state.salesforce = SalesforceConnection(creds, transport=httpx.MockTransport(fake))


async def findings(api, rid: str, statuses: dict[str, str]) -> dict[str, UUID]:
    ids = {}
    async with api[1].begin() as conn:
        unit = (
            (await conn.execute(select(Unit.id).where(Unit.kind == "paragraph").limit(1)))
            .scalars()
            .first()
        )
        for check_id, status in statuses.items():
            fid = (
                await conn.execute(
                    insert(Finding)
                    .values(
                        run_id=UUID(rid),
                        check_id=check_id,
                        status=status,
                        rationale=f"Rationale for {check_id}.",
                        missing_aspects=["meeting frequency"] if check_id == "G1" else [],
                        steps=1,
                        cost_usd=0,
                    )
                    .returning(Finding.id)
                )
            ).scalar_one()
            ids[check_id] = fid
            if check_id == "G1":
                await conn.execute(
                    insert(Evidence).values(
                        finding_id=fid,
                        unit_id=unit,
                        quote="The Board oversees climate risk.",
                        role="supports",
                        verified=True,
                    )
                )
    return ids


EVERYTHING = {
    "G1": "partial_evidence",
    "G2": "inconsistent",
    "S1": "no_evidence_found",
    "M1": "evidence_found",
    "M2": "needs_review",
}


async def linked_run(api, statuses=None) -> tuple[str, str, dict[str, UUID]]:
    r = await run(api)
    response = await api[0].put(f"/packs/{r['pack_id']}/salesforce-account", json=ACCOUNT)
    assert response.status_code == 200, response.text
    return r["pack_id"], r["id"], await findings(api, r["id"], statuses or EVERYTHING)


def outcomes(body: dict) -> dict[str, str]:
    return {row["check_id"]: row["result"] for row in body["results"]}


# 1. Test connection


async def test_connect_success_without_leaking_secrets(api, caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeSalesforce()
    install(api, fake, configured=False)
    before = await api[0].get("/integrations/salesforce")
    assert before.json() == {"configured": False, "instance_url": None, "edition": None}
    response = await api[0].post("/integrations/salesforce/test", json=GOOD)
    assert response.status_code == 200, response.text
    assert response.json() == {"connected": True, "edition": "Developer Edition"}
    status = await api[0].get("/integrations/salesforce")
    assert status.json() == {
        "configured": True,
        "instance_url": INSTANCE,
        "edition": "Developer Edition",
    }
    accounts = await api[0].get("/integrations/salesforce/accounts", params={"q": "Watt"})
    async with api[1].connect() as conn:
        audits = (await conn.execute(select(AuditLog.detail, AuditLog.target))).all()
    texts = [before.text, response.text, status.text, accounts.text, caplog.text, repr(audits)]
    for text in texts:
        assert SECRET not in text
        for token in TOKENS:
            assert token not in text


async def test_connect_rejected_keeps_previous_state(api):
    fake = FakeSalesforce()
    fake.token_status = 400
    install(api, fake, configured=False)
    response = await api[0].post("/integrations/salesforce/test", json=GOOD)
    assert response.status_code == 422
    assert response.json() == {
        "error": "salesforce_rejected",
        "message": "Salesforce rejected the credentials.",
    }
    assert SECRET not in response.text
    assert (await api[0].get("/integrations/salesforce")).json()["configured"] is False


async def test_connect_validates_instance_url(api):
    install(api, FakeSalesforce(), configured=False)
    response = await api[0].post(
        "/integrations/salesforce/test", json={**GOOD, "instance_url": "http://evil.example"}
    )
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_instance_url"
    assert SECRET not in response.text


async def test_settings_override_from_environment(api):
    from cda.config import Settings

    settings = Settings(
        _env_file=None,
        cda_sf_instance_url=INSTANCE + "/",
        cda_sf_client_id="3MVG9-consumer-key",
        cda_sf_client_secret=SECRET,
    )
    connection = SalesforceConnection.from_settings(settings)
    assert connection.status() == {"configured": True, "instance_url": INSTANCE, "edition": None}
    assert SECRET not in repr(settings)


# 2. Account search


async def test_account_search_escapes_the_term(api):
    fake = FakeSalesforce()
    install(api, fake)
    response = await api[0].get(
        "/integrations/salesforce/accounts", params={"q": "O'Brien 100%_\\"}
    )
    assert response.status_code == 200, response.text
    assert response.json() == [
        {"id": "0015j00000WattAAAQ", "name": "Wattlebrook Energy Ltd"},
        {"id": "0015j00000WatsAAAQ", "name": "Wattlebrook Holdings"},
    ]
    query = next(r for r in fake.requests if r.url.path == f"{DATA}/query")
    assert parse_qs(query.url.query.decode())["q"] == [
        "SELECT Id, Name FROM Account WHERE Name LIKE '%O\\'Brien 100\\%\\_\\\\%' LIMIT 10"
    ]


async def test_account_search_no_results(api):
    install(api, FakeSalesforce())
    response = await api[0].get("/integrations/salesforce/accounts", params={"q": "Nobody"})
    assert response.status_code == 200
    assert response.json() == []


async def test_account_search_requires_configuration(api):
    install(api, FakeSalesforce(), configured=False)
    response = await api[0].get("/integrations/salesforce/accounts", params={"q": "Watt"})
    assert response.status_code == 409
    assert response.json()["error"] == "salesforce_not_configured"


# 3. Link / unlink and roles


async def test_link_and_unlink(api):
    pid = await pack(api)
    path = f"/packs/{pid}/salesforce-account"
    response = await api[0].put(path, json=ACCOUNT)
    assert response.status_code == 200, response.text
    assert response.json()["salesforce_account"] == ACCOUNT
    assert (await api[0].get(f"/packs/{pid}")).json()["salesforce_account"] == ACCOUNT
    assert (await api[0].delete(path)).status_code == 200
    assert (await api[0].get(f"/packs/{pid}")).json()["salesforce_account"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"id": "not-an-id", "name": "X"},
        {"id": "5005j00000Case1AAA", "name": "X"},
        {"id": "0015j00000WattAAAQ", "name": ""},
    ],
)
async def test_link_validates_account(api, body):
    pid = await pack(api)
    response = await api[0].put(f"/packs/{pid}/salesforce-account", json=body)
    assert response.status_code == 422


async def test_link_hidden_pack_is_404(api):
    pid = await pack(api, "Financial Services")
    response = await api[0].put(
        f"/packs/{pid}/salesforce-account", json=ACCOUNT, headers={"X-Demo-User": "alex"}
    )
    assert response.status_code == 404


@pytest.mark.parametrize("operation", ["test", "accounts", "link", "unlink", "push"])
async def test_alex_forbidden(api, operation):
    fake = FakeSalesforce()
    install(api, fake)
    pid, rid, _ = await linked_run(api)
    alex = {"X-Demo-User": "alex"}
    if operation == "test":
        response = await api[0].post("/integrations/salesforce/test", json=GOOD, headers=alex)
    elif operation == "accounts":
        response = await api[0].get(
            "/integrations/salesforce/accounts", params={"q": "W"}, headers=alex
        )
    elif operation == "link":
        response = await api[0].put(f"/packs/{pid}/salesforce-account", json=ACCOUNT, headers=alex)
    elif operation == "unlink":
        response = await api[0].delete(f"/packs/{pid}/salesforce-account", headers=alex)
    else:
        response = await api[0].post(f"/runs/{rid}/salesforce/cases", headers=alex)
    assert response.status_code == 403
    assert fake.requests == []
    # Reading the status is allowed for everyone who can see the run.
    assert (await api[0].get("/integrations/salesforce", headers=alex)).status_code == 200
    assert (await api[0].get(f"/runs/{rid}/salesforce/cases", headers=alex)).status_code == 200


# 4. Cases


async def test_cases_created_once_then_new_eligible_after_review(api):
    fake = FakeSalesforce()
    install(api, fake)
    _, rid, ids = await linked_run(api)
    response = await api[0].post(f"/runs/{rid}/salesforce/cases")
    assert response.status_code == 200, response.text
    body = response.json()
    assert outcomes(body) == {"G1": "created", "G2": "created", "S1": "created"}
    assert [c["Subject"] for c in fake.created] == [
        "G1 · Board oversight of climate-related risks and opportunities",
        "G2 · Management's role in climate-related risks and opportunities",
        "S1 · Climate-related risks and opportunities and time horizons",
    ]
    for case in fake.created:
        assert case["AccountId"] == ACCOUNT["id"]
        assert case["Origin"] == "Climate Disclosure Agent"
        assert f"/runs/{rid}/report" in case["Description"]
    g1 = fake.created[0]["Description"]
    assert "Status: Partial evidence" in g1
    assert "Rationale for G1." in g1
    assert "meeting frequency" in g1
    assert 'Quote: "The Board oversees climate risk."' in g1
    assert "report.pdf" in g1 and "page 1" in g1
    assert "Status: Inconsistent" in fake.created[1]["Description"]
    assert "No evidence was cited." in fake.created[2]["Description"]
    g1_row = body["results"][0]
    assert g1_row["finding_id"] == str(ids["G1"])
    assert g1_row["case_id"] == "5005j00000Case001A"
    assert g1_row["case_number"] == "00000001"
    assert g1_row["case_url"] == f"{INSTANCE}/lightning/r/Case/5005j00000Case001A/view"

    again = await api[0].post(f"/runs/{rid}/salesforce/cases")
    assert again.status_code == 200
    assert outcomes(again.json()) == {"G1": "exists", "G2": "exists", "S1": "exists"}
    assert fake.creates == 3

    review = await api[0].post(
        f"/findings/{ids['M1']}/reviews",
        json={
            "decision": "change",
            "new_status": "partial_evidence",
            "note": "Scope 3 is missing.",
            "version": 1,
        },
    )
    assert review.status_code == 200, review.text
    third = await api[0].post(f"/runs/{rid}/salesforce/cases")
    assert outcomes(third.json()) == {
        "G1": "exists",
        "G2": "exists",
        "S1": "exists",
        "M1": "created",
    }
    assert fake.creates == 4
    assert "Status: Partial evidence" in fake.created[3]["Description"]
    async with api[1].connect() as conn:
        stored = (await conn.execute(select(SalesforceCase.finding_id))).scalars().all()
    assert sorted(stored) == sorted([ids["G1"], ids["G2"], ids["S1"], ids["M1"]])


async def test_review_back_to_found_is_not_eligible(api):
    fake = FakeSalesforce()
    install(api, fake)
    _, rid, ids = await linked_run(api, {"G1": "partial_evidence", "G2": "no_evidence_found"})
    await api[0].post(
        f"/findings/{ids['G2']}/reviews",
        json={
            "decision": "change",
            "new_status": "evidence_found",
            "note": "Found on page 4 of the report.",
            "version": 1,
        },
    )
    response = await api[0].post(f"/runs/{rid}/salesforce/cases")
    assert outcomes(response.json()) == {"G1": "created"}


# 5. One failure


async def test_one_case_fails_then_retry_creates_only_it(api):
    fake = FakeSalesforce()
    fake.fail_checks = {"G2"}
    install(api, fake)
    _, rid, _ = await linked_run(api)
    response = await api[0].post(f"/runs/{rid}/salesforce/cases")
    assert response.status_code == 200
    body = response.json()
    assert outcomes(body) == {"G1": "created", "G2": "failed", "S1": "created"}
    failed = body["results"][1]
    assert failed["case_id"] is None
    assert failed["error"]["code"] == "INVALID_OR_NULL_FOR_RESTRICTED_PICKLIST"
    assert "restricted picklist" in failed["error"]["message"]
    fake.fail_checks = set()
    retry = await api[0].post(f"/runs/{rid}/salesforce/cases")
    assert outcomes(retry.json()) == {"G1": "exists", "G2": "created", "S1": "exists"}
    assert len(fake.created) == 3
    assert fake.creates == 4


# 6. Limits and tokens


async def test_limit_stops_and_keeps_created_cases(api):
    fake = FakeSalesforce()
    fake.limit_after = 1
    install(api, fake)
    _, rid, ids = await linked_run(api)
    response = await api[0].post(f"/runs/{rid}/salesforce/cases")
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "salesforce_limit"
    assert body["message"] == "Salesforce API limit reached. Try again later."
    assert outcomes(body) == {"G1": "created", "G2": "failed", "S1": "failed"}
    assert body["results"][2]["error"]["code"] == "salesforce_limit"
    assert fake.creates == 2  # nothing attempted after the limit
    async with api[1].connect() as conn:
        stored = (await conn.execute(select(SalesforceCase.finding_id))).scalars().all()
    assert stored == [ids["G1"]]
    fake.limit_after = None
    retry = await api[0].post(f"/runs/{rid}/salesforce/cases")
    assert outcomes(retry.json()) == {"G1": "exists", "G2": "created", "S1": "created"}


async def test_http_429_is_a_limit(api):
    fake = FakeSalesforce()
    install(api, fake)
    _, rid, _ = await linked_run(api, {"G1": "partial_evidence"})

    def limited(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{DATA}/sobjects/Case":
            fake.requests.append(request)
            return httpx.Response(429, json=[{"errorCode": "TOO_MANY_REQUESTS", "message": "x"}])
        return fake(request)

    api[2].state.salesforce = SalesforceConnection(
        Credentials(INSTANCE, "3MVG9-consumer-key", SecretStr(SECRET)),
        transport=httpx.MockTransport(limited),
    )
    response = await api[0].post(f"/runs/{rid}/salesforce/cases")
    assert response.status_code == 503
    assert response.json()["error"] == "salesforce_limit"


async def test_expired_token_refreshed_once(api):
    fake = FakeSalesforce()
    install(api, fake)
    _, rid, _ = await linked_run(api, {"G1": "partial_evidence"})
    # Warm the token, then let it expire.
    assert (
        await api[0].get("/integrations/salesforce/accounts", params={"q": "W"})
    ).status_code == 200
    fake.unauthorised = 1
    response = await api[0].post(f"/runs/{rid}/salesforce/cases")
    assert response.status_code == 200, response.text
    assert outcomes(response.json()) == {"G1": "created"}
    assert fake.count("/services/oauth2/token") == 2
    assert fake.creates == 2


async def test_unauthorised_after_refresh_is_rejected(api):
    fake = FakeSalesforce()
    install(api, fake)
    _, rid, _ = await linked_run(api)
    fake.unauthorised = 100
    response = await api[0].post(f"/runs/{rid}/salesforce/cases")
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "salesforce_rejected"
    assert outcomes(body) == {"G1": "failed", "G2": "failed", "S1": "failed"}
    assert fake.count("/services/oauth2/token") == 2
    assert fake.creates == 2  # one call, one retry after the refresh, then stop
    assert SECRET not in response.text


# 7. ready reasons


async def test_ready_reasons(api):
    fake = FakeSalesforce()
    install(api, fake, configured=False)
    r = await run(api)
    rid, pid = r["id"], r["pack_id"]
    path = f"/runs/{rid}/salesforce/cases"
    assert (await api[0].get(path)).json()["reason"] == "not_configured"

    install(api, fake)
    assert (await api[0].get(path)).json()["reason"] == "not_linked"

    await api[0].put(f"/packs/{pid}/salesforce-account", json=ACCOUNT)
    empty = (await api[0].get(path)).json()
    assert empty == {"ready": False, "reason": "nothing_to_send", "findings": []}

    ids = await findings(api, rid, {"G1": "partial_evidence", "M1": "evidence_found"})
    ready = (await api[0].get(path)).json()
    assert ready["ready"] is True
    assert ready["reason"] is None
    assert ready["findings"] == [
        {
            "finding_id": str(ids["G1"]),
            "check_id": "G1",
            "status": "partial_evidence",
            "case_id": None,
            "case_number": None,
            "case_url": None,
        }
    ]
    assert (await api[0].post(path)).status_code == 200
    sent = (await api[0].get(path)).json()
    assert sent["ready"] is False
    assert sent["reason"] == "nothing_to_send"
    assert sent["findings"][0]["case_number"] == "00000001"
    assert sent["findings"][0]["case_url"].endswith("/lightning/r/Case/5005j00000Case001A/view")


@pytest.mark.parametrize("state", ["not_configured", "not_linked"])
async def test_push_requires_configuration_and_link(api, state):
    fake = FakeSalesforce()
    install(api, fake, configured=state != "not_configured")
    r = await run(api)
    await findings(api, r["id"], {"G1": "partial_evidence"})
    if state == "not_configured":
        await api[0].put(f"/packs/{r['pack_id']}/salesforce-account", json=ACCOUNT)
    response = await api[0].post(f"/runs/{r['id']}/salesforce/cases")
    assert response.status_code == 409
    assert response.json()["error"] == f"salesforce_{state}"
    assert fake.requests == []


async def test_hidden_run_is_404(api):
    install(api, FakeSalesforce())
    r = await run(api, await _financial_pack(api))
    alex = {"X-Demo-User": "alex"}
    path = f"/runs/{r['id']}/salesforce/cases"
    assert (await api[0].get(path, headers=alex)).status_code == 404
    assert (await api[0].post(path, headers=alex)).status_code == 404


async def _financial_pack(api):
    from test_api import indexed

    return await indexed(api, "Financial Services")
