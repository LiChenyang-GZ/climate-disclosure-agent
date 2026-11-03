"""Microsoft Graph: device-code sign-in and read-only OneDrive / SharePoint access (X2, D84).

Tokens live only in API memory (`MicrosoftState`, one connection per demo user); they are never
written to the database, files, logs or API responses.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import quote, unquote

import httpx

LOGIN_URL = "https://login.microsoftonline.com"
GRAPH_URL = "https://graph.microsoft.com/v1.0"
SCOPES = "Files.Read.All Sites.Read.All offline_access User.Read"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
SUPPORTED_SUFFIXES = (".pdf", ".docx", ".xlsx")
TIMEOUT_SECONDS = 30.0
MAX_RETRY_AFTER = 60.0
MAX_PAGES = 50
# Refresh this many seconds before the access token runs out.
REFRESH_MARGIN = 60.0

PollState = Literal["pending", "cancelled", "expired"]
TokenSource = Callable[[bool], Awaitable[str]]


class ExpiredError(Exception):
    """The connection can no longer be used (Graph 401 after a refresh, or refresh refused)."""


class LoginError(Exception):
    """Microsoft sign-in answered something unexpected (bad client id, outage)."""


class GraphError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"Microsoft Graph request failed ({status})")
        self.status = status


class DeniedError(GraphError):
    pass


class NotFoundError(GraphError):
    pass


class TooLargeError(GraphError):
    pass


class _RedactQuery(logging.Filter):
    """httpx logs request URLs; download URLs carry a `tempauth` token in their query."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                arg.copy_with(query=None) if isinstance(arg, httpx.URL) and arg.query else arg
                for arg in record.args
            )
        return True


_httpx_logger = logging.getLogger("httpx")
if not any(isinstance(f, _RedactQuery) for f in _httpx_logger.filters):
    _httpx_logger.addFilter(_RedactQuery())


@dataclass(frozen=True, repr=False)
class Tokens:
    access_token: str
    refresh_token: str | None
    expires_at: float

    def __repr__(self) -> str:
        return "Tokens(<redacted>)"


@dataclass(frozen=True)
class DeviceCode:
    device_code: str = field(repr=False)
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int
    expires_at: float


def supported(name: str | None) -> bool:
    return PurePosixPath(name or "").suffix.lower() in SUPPORTED_SUFFIXES


def item_path(item: dict[str, Any]) -> str:
    """Path inside the drive, e.g. `/Drafts/report.pdf` (parentReference.path is `…/root:/Drafts`)."""
    parent = (item.get("parentReference") or {}).get("path") or ""
    folder = unquote(parent.partition("root:")[2]).rstrip("/")
    return f"{folder}/{item.get('name', '')}"


def _segment(value: str) -> str:
    return quote(value, safe="!,")


async def _form(http: httpx.AsyncClient, url: str, data: dict[str, str]) -> httpx.Response:
    try:
        return await http.post(url, data=data, timeout=TIMEOUT_SECONDS)
    except httpx.HTTPError:
        raise LoginError("Microsoft sign-in is unavailable") from None


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


class MicrosoftLogin:
    """OAuth 2.0 device-code flow against the Microsoft identity platform (public client)."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        tenant: str,
        client_id: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.http = http
        self.client_id = client_id
        self.clock = clock
        self.base = f"{LOGIN_URL}/{_segment(tenant)}/oauth2/v2.0"

    async def device_code(self) -> DeviceCode:
        response = await _form(
            self.http, f"{self.base}/devicecode", {"client_id": self.client_id, "scope": SCOPES}
        )
        body = _json(response)
        if response.status_code != 200 or not isinstance(body.get("device_code"), str):
            raise LoginError("Microsoft sign-in refused the request")
        expires_in = int(body.get("expires_in", 900))
        return DeviceCode(
            device_code=body["device_code"],
            user_code=str(body["user_code"]),
            verification_uri=str(body["verification_uri"]),
            expires_in=expires_in,
            interval=int(body.get("interval", 5)),
            expires_at=self.clock() + expires_in,
        )

    async def poll(self, device_code: str) -> Tokens | PollState:
        response = await _form(
            self.http,
            f"{self.base}/token",
            {"grant_type": DEVICE_GRANT, "client_id": self.client_id, "device_code": device_code},
        )
        if response.status_code == 200:
            return self._tokens(_json(response), None)
        error = _json(response).get("error")
        if error in ("authorization_pending", "slow_down"):
            return "pending"
        if error == "authorization_declined":
            return "cancelled"
        if error == "expired_token":
            return "expired"
        raise LoginError("Microsoft sign-in failed")

    async def refresh(self, refresh_token: str) -> Tokens:
        try:
            response = await _form(
                self.http,
                f"{self.base}/token",
                {
                    "grant_type": "refresh_token",
                    "client_id": self.client_id,
                    "refresh_token": refresh_token,
                    "scope": SCOPES,
                },
            )
        except LoginError:
            raise ExpiredError from None
        if response.status_code != 200:
            raise ExpiredError
        return self._tokens(_json(response), refresh_token)

    def _tokens(self, body: dict[str, Any], previous_refresh: str | None) -> Tokens:
        access = body.get("access_token")
        if not isinstance(access, str) or not access:
            raise LoginError("Microsoft sign-in returned no access token")
        refresh = body.get("refresh_token")
        return Tokens(
            access_token=access,
            refresh_token=refresh if isinstance(refresh, str) and refresh else previous_refresh,
            expires_at=self.clock() + float(body.get("expires_in", 3600)),
        )


def _retry_after(response: httpx.Response) -> float:
    try:
        seconds = float(response.headers.get("retry-after", "1"))
    except ValueError:
        seconds = 1.0
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)


def _raise_for(status: int) -> None:
    if status == 403:
        raise DeniedError(status)
    if status == 404:
        raise NotFoundError(status)
    raise GraphError(status)


class GraphClient:
    """GET-only Graph calls. `token(force)` returns an access token, refreshing when forced."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        token: TokenSource,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.http = http
        self.token = token
        self.sleep = sleep

    async def _send(self, url: str) -> httpx.Response:
        """Streamed response with status < 400; the caller closes it."""
        if not url.startswith(GRAPH_URL + "/"):
            raise GraphError(0)
        refreshed = retried = False
        while True:
            token = await self.token(refreshed)
            request = self.http.build_request(
                "GET", url, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT_SECONDS
            )
            try:
                response = await self.http.send(request, stream=True)
            except httpx.HTTPError:
                raise GraphError(0) from None
            status = response.status_code
            if status < 400:
                return response
            await response.aclose()
            if status == 401:
                if refreshed:
                    raise ExpiredError
                refreshed = True
            elif status == 429 and not retried:
                retried = True
                await self.sleep(_retry_after(response))
            else:
                _raise_for(status)

    async def get(self, path: str) -> dict[str, Any]:
        url = path if path.startswith("https://") else GRAPH_URL + path
        response = await self._send(url)
        try:
            await response.aread()
        except httpx.HTTPError:
            raise GraphError(0) from None
        finally:
            await response.aclose()
        if response.status_code != 200:
            raise GraphError(response.status_code)
        return _json(response)

    async def get_all(self, path: str) -> list[dict[str, Any]]:
        """Every `value` across `@odata.nextLink` pages."""
        items: list[dict[str, Any]] = []
        url: str | None = path
        for _ in range(MAX_PAGES):
            if url is None:
                return items
            page = await self.get(url)
            items.extend(v for v in page.get("value", []) if isinstance(v, dict))
            next_link = page.get("@odata.nextLink")
            url = next_link if isinstance(next_link, str) else None
        if url is not None:
            raise GraphError(0)
        return items

    async def me(self) -> dict[str, Any]:
        return await self.get("/me")

    async def my_root_children(self) -> list[dict[str, Any]]:
        return await self.get_all("/me/drive/root/children")

    async def sites(self) -> list[dict[str, Any]]:
        return await self.get_all("/sites?search=*")

    async def site_drives(self, site_id: str) -> list[dict[str, Any]]:
        return await self.get_all(f"/sites/{_segment(site_id)}/drives")

    async def children(self, drive_id: str, item_id: str) -> list[dict[str, Any]]:
        return await self.get_all(
            f"/drives/{_segment(drive_id)}/items/{_segment(item_id)}/children"
        )

    async def item(self, drive_id: str, item_id: str) -> dict[str, Any]:
        return await self.get(f"/drives/{_segment(drive_id)}/items/{_segment(item_id)}")

    async def content(self, drive_id: str, item_id: str, *, limit: int) -> bytes:
        """File bytes, at most `limit`. Graph answers 302 to a pre-authenticated download URL,
        which is fetched without the bearer token."""
        response = await self._send(
            f"{GRAPH_URL}/drives/{_segment(drive_id)}/items/{_segment(item_id)}/content"
        )
        try:
            if response.is_redirect:
                location = response.headers.get("location", "")
                await response.aclose()
                if not location.startswith("https://"):
                    raise GraphError(response.status_code)
                try:
                    response = await self.http.send(
                        self.http.build_request("GET", location, timeout=TIMEOUT_SECONDS),
                        stream=True,
                    )
                except httpx.HTTPError:
                    raise GraphError(0) from None
                if response.status_code >= 400:
                    _raise_for(response.status_code)
            if response.status_code != 200:
                raise GraphError(response.status_code)
            data = bytearray()
            try:
                async for chunk in response.aiter_bytes():
                    data += chunk
                    if len(data) > limit:
                        raise TooLargeError(413)
            except httpx.HTTPError:
                raise GraphError(0) from None
            return bytes(data)
        finally:
            await response.aclose()


@dataclass
class Connection:
    tokens: Tokens
    account: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def access_token(
        self, login: MicrosoftLogin, clock: Callable[[], float], force: bool
    ) -> str:
        async with self.lock:
            if force or self.tokens.expires_at - REFRESH_MARGIN <= clock():
                if not self.tokens.refresh_token:
                    raise ExpiredError
                self.tokens = await login.refresh(self.tokens.refresh_token)
            return self.tokens.access_token


@dataclass
class MicrosoftState:
    """In-memory sign-in state per demo user; an API restart forgets it (D84)."""

    transport: httpx.AsyncBaseTransport | None = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic
    pending: dict[str, DeviceCode] = field(default_factory=dict)
    connections: dict[str, Connection] = field(default_factory=dict)

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport, timeout=TIMEOUT_SECONDS)
