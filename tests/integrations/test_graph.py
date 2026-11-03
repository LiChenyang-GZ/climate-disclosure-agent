"""Microsoft Graph client and device-code sign-in against recorded-style responses (X2)."""

import json
import logging
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from cda.integrations.graph import (
    DeniedError,
    ExpiredError,
    GraphClient,
    GraphError,
    MicrosoftLogin,
    NotFoundError,
    Tokens,
    TooLargeError,
    item_path,
    supported,
)

FIXTURES = Path(__file__).parent / "fixtures" / "graph"
GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com/organizations/oauth2/v2.0"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text("utf-8"))


def reply(status: int, name: str | None = None, **kwargs) -> httpx.Response:
    return httpx.Response(status, json=fixture(name) if name else None, **kwargs)


class Recorder:
    """MockTransport handler that answers from a list of (match, response) pairs."""

    def __init__(self, routes):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for match, response in self.routes:
            if match(request):
                return response(request) if callable(response) else response
        raise AssertionError(f"unexpected request {request.method} {request.url}")


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def graph(http: httpx.AsyncClient, sleeps: list[float] | None = None, tokens=None) -> GraphClient:
    issued = tokens if tokens is not None else []

    async def token(force: bool) -> str:
        issued.append(force)
        return "FAKE-ACCESS-TOKEN-2" if force else "FAKE-ACCESS-TOKEN-1"

    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return GraphClient(http, token, sleep=sleep)


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


# --- device-code sign-in ----------------------------------------------------------------------


async def test_device_code_then_poll_states():
    polls = iter(
        [
            reply(400, "error_authorization_pending"),
            reply(200, "token"),
        ]
    )
    handler = Recorder(
        [
            (lambda r: r.url.path.endswith("/devicecode"), reply(200, "devicecode")),
            (lambda r: r.url.path.endswith("/token"), lambda r: next(polls)),
        ]
    )
    async with client(handler) as http:
        login = MicrosoftLogin(http, tenant="organizations", client_id="cid", clock=lambda: 100.0)
        code = await login.device_code()
        assert (code.user_code, code.verification_uri, code.expires_in, code.interval) == (
            "FAKE7Q2K",
            "https://microsoft.com/devicelogin",
            900,
            5,
        )
        assert "FAKE-DEVICE-CODE" not in repr(code)
        assert await login.poll(code.device_code) == "pending"
        tokens = await login.poll(code.device_code)
    assert isinstance(tokens, Tokens)
    assert tokens.access_token == "FAKE-ACCESS-TOKEN-1"
    assert tokens.refresh_token == "FAKE-REFRESH-TOKEN-1"
    assert tokens.expires_at == 100.0 + 3599
    assert "FAKE" not in repr(tokens)
    start, *polled = handler.requests
    assert str(start.url) == f"{LOGIN}/devicecode"
    assert form(start) == {
        "client_id": "cid",
        "scope": "Files.Read.All Sites.Read.All offline_access User.Read",
    }
    assert form(polled[0]) == {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": "cid",
        "device_code": "FAKE-DEVICE-CODE-never-shown",
    }


@pytest.mark.parametrize(
    ("fixture_name", "state"),
    [
        ("error_authorization_declined", "cancelled"),
        ("error_expired_token", "expired"),
        ("error_authorization_pending", "pending"),
    ],
)
async def test_poll_maps_errors(fixture_name, state):
    handler = Recorder([(lambda r: True, reply(400, fixture_name))])
    async with client(handler) as http:
        login = MicrosoftLogin(http, tenant="organizations", client_id="cid")
        assert await login.poll("FAKE-DEVICE-CODE") == state


async def test_slow_down_is_pending():
    handler = Recorder([(lambda r: True, httpx.Response(400, json={"error": "slow_down"}))])
    async with client(handler) as http:
        login = MicrosoftLogin(http, tenant="organizations", client_id="cid")
        assert await login.poll("FAKE-DEVICE-CODE") == "pending"


async def test_refresh_success_keeps_old_refresh_token_when_omitted():
    body = fixture("token_refreshed")
    del body["refresh_token"]
    handler = Recorder([(lambda r: True, httpx.Response(200, json=body))])
    async with client(handler) as http:
        login = MicrosoftLogin(http, tenant="contoso", client_id="cid", clock=lambda: 0.0)
        tokens = await login.refresh("FAKE-REFRESH-TOKEN-1")
    assert tokens.access_token == "FAKE-ACCESS-TOKEN-2"
    assert tokens.refresh_token == "FAKE-REFRESH-TOKEN-1"
    assert str(handler.requests[0].url) == (
        "https://login.microsoftonline.com/contoso/oauth2/v2.0/token"
    )
    assert form(handler.requests[0])["grant_type"] == "refresh_token"


async def test_refresh_failure_is_expired():
    handler = Recorder([(lambda r: True, reply(400, "error_invalid_grant"))])
    async with client(handler) as http:
        login = MicrosoftLogin(http, tenant="organizations", client_id="cid")
        with pytest.raises(ExpiredError):
            await login.refresh("FAKE-REFRESH-TOKEN-1")


# --- Graph reads ------------------------------------------------------------------------------


async def test_paging_follows_next_link_with_bearer_token():
    handler = Recorder(
        [
            (lambda r: "skiptoken" in str(r.url), reply(200, "sites_page2")),
            (lambda r: r.url.path == "/v1.0/sites", reply(200, "sites_page1")),
        ]
    )
    async with client(handler) as http:
        sites = await graph(http).sites()
    assert [s["name"] for s in sites] == ["Sustainability", "Finance"]
    assert handler.requests[0].url.params["search"] == "*"
    assert all(r.headers["authorization"] == "Bearer FAKE-ACCESS-TOKEN-1" for r in handler.requests)


async def test_next_link_to_another_host_is_refused():
    page = fixture("sites_page1")
    page["@odata.nextLink"] = "https://evil.example.test/steal"
    handler = Recorder([(lambda r: True, httpx.Response(200, json=page))])
    async with client(handler) as http:
        with pytest.raises(GraphError):
            await graph(http).sites()
    assert len(handler.requests) == 1


async def test_429_retries_once_after_retry_after():
    responses = iter([reply(429, "error_429", headers={"Retry-After": "3"}), reply(200, "me")])
    handler = Recorder([(lambda r: True, lambda r: next(responses))])
    sleeps: list[float] = []
    async with client(handler) as http:
        me = await graph(http, sleeps).me()
    assert me["userPrincipalName"] == "sarah.chen@wattlebrook.example"
    assert sleeps == [3.0]


async def test_429_twice_is_an_error():
    handler = Recorder([(lambda r: True, reply(429, "error_429", headers={"Retry-After": "1"}))])
    sleeps: list[float] = []
    async with client(handler) as http:
        with pytest.raises(GraphError) as raised:
            await graph(http, sleeps).me()
    assert raised.value.status == 429
    assert len(handler.requests) == 2
    assert sleeps == [1.0]


async def test_401_refreshes_once_then_expires():
    responses = iter([reply(401, "error_401"), reply(200, "me")])
    handler = Recorder([(lambda r: True, lambda r: next(responses))])
    issued: list[bool] = []
    async with client(handler) as http:
        await graph(http, tokens=issued).me()
    assert issued == [False, True]
    assert handler.requests[1].headers["authorization"] == "Bearer FAKE-ACCESS-TOKEN-2"

    handler = Recorder([(lambda r: True, reply(401, "error_401"))])
    async with client(handler) as http:
        with pytest.raises(ExpiredError):
            await graph(http).me()
    assert len(handler.requests) == 2


@pytest.mark.parametrize(
    ("status", "error"), [(403, DeniedError), (404, NotFoundError), (500, GraphError)]
)
async def test_error_statuses(status, error):
    handler = Recorder([(lambda r: True, httpx.Response(status, json={"error": {}}))])
    async with client(handler) as http:
        with pytest.raises(error):
            await graph(http).item("b!drive", "01ITEM")


async def test_network_failure_is_graph_error():
    def fail(request):
        raise httpx.ConnectError("offline", request=request)

    async with client(fail) as http:
        with pytest.raises(GraphError):
            await graph(http).me()


async def test_children_paging_and_ids_are_quoted():
    handler = Recorder(
        [
            (lambda r: "skiptoken" in str(r.url), reply(200, "drafts_children_page2")),
            (lambda r: True, reply(200, "drafts_children_page1")),
        ]
    )
    async with client(handler) as http:
        items = await graph(http).children("b!sustainability-documents", "01DRAFTSFOLDER")
    assert len(items) == 6
    assert handler.requests[0].url.raw_path == (
        b"/v1.0/drives/b!sustainability-documents/items/01DRAFTSFOLDER/children"
    )


async def test_content_follows_redirect_without_authorization():
    handler = Recorder(
        [
            (
                lambda r: r.url.host == "graph.microsoft.com",
                httpx.Response(
                    302,
                    headers={"Location": "https://download.example.test/f?tempauth=FAKE-TEMPAUTH"},
                ),
            ),
            (
                lambda r: r.url.host == "download.example.test",
                httpx.Response(200, content=b"%PDF-1"),
            ),
        ]
    )
    async with client(handler) as http:
        data = await graph(http).content("b!drive", "01ITEM", limit=100)
    assert data == b"%PDF-1"
    first, second = handler.requests
    assert first.url.path == "/v1.0/drives/b!drive/items/01ITEM/content"
    assert first.headers["authorization"] == "Bearer FAKE-ACCESS-TOKEN-1"
    assert "authorization" not in second.headers


async def test_content_size_limit():
    handler = Recorder([(lambda r: True, httpx.Response(200, content=b"x" * 101))])
    async with client(handler) as http:
        with pytest.raises(TooLargeError):
            await graph(http).content("b!drive", "01ITEM", limit=100)


async def test_download_url_query_is_not_logged(caplog):
    caplog.set_level(logging.DEBUG)
    handler = Recorder(
        [
            (
                lambda r: r.url.host == "graph.microsoft.com",
                httpx.Response(
                    302,
                    headers={"Location": "https://download.example.test/f?tempauth=FAKE-TEMPAUTH"},
                ),
            ),
            (lambda r: True, httpx.Response(200, content=b"%PDF-1")),
        ]
    )
    async with client(handler) as http:
        await graph(http).content("b!drive", "01ITEM", limit=100)
    assert "download.example.test" in caplog.text
    assert "FAKE-TEMPAUTH" not in caplog.text
    assert "FAKE-ACCESS-TOKEN" not in caplog.text


def test_supported_and_path():
    assert supported("Report.PDF") and supported("a.docx") and supported("b.xlsx")
    assert not supported("deck.pptx") and not supported("notes") and not supported(None)
    page = fixture("drafts_children_page1")["value"]
    assert item_path(page[1]) == "/Drafts/climate-report-2026.pdf"
    root = fixture("my_drive_root_children")["value"][1]
    assert item_path(root) == "/Climate notes.docx"
