"""Salesforce REST client for pushing follow-ups as Cases (D85).

Client credentials flow of a connected app; REST API v62.0. Credentials and access
tokens live only in memory: they are never logged, stored or returned by the API.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import SecretStr

from cda.config import Settings

API_VERSION = "v62.0"
ORIGIN = "Climate Disclosure Agent"
SUBJECT_MAX = 255
# Case.Description is a long text area of 32,000 characters.
DESCRIPTION_MAX = 32_000
ELIGIBLE_STATUSES = ("inconsistent", "partial_evidence", "no_evidence_found")

_ID = re.compile(r"[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?")
_STATUS_LABELS = {
    "evidence_found": "Evidence found",
    "partial_evidence": "Partial evidence",
    "no_evidence_found": "No evidence found",
    "inconsistent": "Inconsistent",
    "needs_review": "Needs review",
    "not_checked": "Not checked",
    "error": "Error",
}

log = logging.getLogger(__name__)


class SalesforceError(Exception):
    """Base error; messages are fixed or come from Salesforce error bodies, never secrets."""

    code = "salesforce_error"
    default_message = "Salesforce returned an error."

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class SalesforceRejected(SalesforceError):
    """Credentials or token rejected (invalid client, or still 401 after a refresh)."""

    code = "salesforce_rejected"
    default_message = "Salesforce rejected the credentials."


class SalesforceLimit(SalesforceError):
    """REQUEST_LIMIT_EXCEEDED or HTTP 429."""

    code = "salesforce_limit"
    default_message = "Salesforce API limit reached. Try again later."


class SalesforceUnavailable(SalesforceError):
    """Network failure, 5xx, or a response we cannot read."""

    code = "salesforce_unavailable"
    default_message = "Couldn't reach Salesforce. Try again later."


class SalesforceRequestFailed(SalesforceError):
    """Salesforce refused one request, e.g. a validation rule on the Case."""

    def __init__(self, status: int, error_code: str, message: str) -> None:
        self.status = status
        self.error_code = error_code
        super().__init__(message)


def normalize_instance_url(value: str) -> str:
    """Return `https://host[:port]`; anything else (path, query, credentials) is refused."""
    try:
        url = httpx.URL(value.strip())
    except httpx.InvalidURL as exc:
        raise ValueError("Invalid Salesforce instance URL") from exc
    if (
        url.scheme != "https"
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
        or url.path not in ("", "/")
    ):
        raise ValueError("The Salesforce instance URL must be an https origin")
    port = f":{url.port}" if url.port else ""
    return f"https://{url.host.lower()}{port}"


@dataclass(frozen=True)
class Credentials:
    instance_url: str
    client_id: str = field(repr=False)
    client_secret: SecretStr = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "instance_url", normalize_instance_url(self.instance_url))


_SOQL_ESCAPES = {
    "\\": "\\\\",
    "'": "\\'",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
    # LIKE wildcards: match them literally.
    "%": "\\%",
    "_": "\\_",
}


def escape_like(term: str) -> str:
    """Escape a user term for use inside a quoted SOQL LIKE pattern."""
    return "".join(_SOQL_ESCAPES.get(ch, ch) for ch in term)


def account_search_soql(term: str) -> str:
    return f"SELECT Id, Name FROM Account WHERE Name LIKE '%{escape_like(term)}%' LIMIT 10"


def _errors(response: httpx.Response) -> tuple[str, str]:
    """(errorCode, message) of the first error in a REST error body."""
    try:
        body = response.json()
    except ValueError:
        return "", ""
    first = body[0] if isinstance(body, list) and body else body
    if not isinstance(first, dict):
        return "", ""
    code = first.get("errorCode") or first.get("error") or ""
    message = first.get("message") or first.get("error_description") or ""
    return str(code)[:100], str(message)[:500]


def _api_error(response: httpx.Response) -> SalesforceError:
    code, message = _errors(response)
    status = response.status_code
    if status == 429 or code == "REQUEST_LIMIT_EXCEEDED":
        return SalesforceLimit()
    if status >= 500:
        return SalesforceUnavailable()
    if status == 401:
        return SalesforceRejected()
    return SalesforceRequestFailed(
        status, code or f"HTTP_{status}", message or SalesforceError.default_message
    )


class SalesforceClient:
    """One connected app's REST access; caches the access token and refreshes it once on 401."""

    def __init__(
        self,
        credentials: Credentials,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.credentials = credentials
        self._transport = transport
        self._timeout = timeout
        self._token: str | None = None
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"SalesforceClient({self.credentials.instance_url!r})"

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout, follow_redirects=False
            ) as http:
                return await http.request(method, url, **kwargs)
        except httpx.HTTPError:
            # Drop the cause: it carries the request, which may hold a token header.
            raise SalesforceUnavailable() from None

    async def _access_token(self, stale: str | None = None) -> str:
        """The cached token, or a new one when there is none or `stale` was rejected."""
        async with self._lock:
            if self._token is not None and self._token != stale:
                return self._token
            self._token = None
            response = await self._send(
                "POST",
                f"{self.credentials.instance_url}/services/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.credentials.client_id,
                    "client_secret": self.credentials.client_secret.get_secret_value(),
                },
            )
            if response.status_code == 429:
                raise SalesforceLimit()
            if response.status_code >= 500:
                raise SalesforceUnavailable()
            if response.status_code != 200:
                code, _ = _errors(response)
                log.info("Salesforce token request refused (%s)", code or response.status_code)
                raise SalesforceRejected()
            try:
                token = response.json().get("access_token")
            except (ValueError, AttributeError):
                token = None
            if not isinstance(token, str) or not token:
                raise SalesforceUnavailable()
            self._token = token
            return token

    async def _api(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.credentials.instance_url}/services/data/{API_VERSION}/{path}"
        token = await self._access_token()
        response = await self._send(
            method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
        if response.status_code == 401:
            token = await self._access_token(stale=token)
            response = await self._send(
                method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs
            )
        if not response.is_success:
            raise _api_error(response)
        try:
            return response.json()
        except ValueError:
            raise SalesforceUnavailable() from None

    async def _records(self, soql: str) -> list[dict[str, Any]]:
        body = await self._api("GET", "query", params={"q": soql})
        records = body.get("records") if isinstance(body, dict) else None
        if not isinstance(records, list):
            raise SalesforceUnavailable()
        return [r for r in records if isinstance(r, dict)]

    async def edition(self) -> str | None:
        """The org's edition, e.g. "Developer Edition"; also proves the credentials work."""
        records = await self._records("SELECT OrganizationType FROM Organization LIMIT 1")
        value = records[0].get("OrganizationType") if records else None
        return value if isinstance(value, str) else None

    async def search_accounts(self, term: str) -> list[dict[str, str]]:
        records = await self._records(account_search_soql(term))
        return [
            {"id": r["Id"], "name": r["Name"]}
            for r in records
            if isinstance(r.get("Id"), str) and isinstance(r.get("Name"), str)
        ]

    async def create_case(self, fields: dict[str, str]) -> str:
        body = await self._api("POST", "sobjects/Case", json=fields)
        case_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(case_id, str) or not _ID.fullmatch(case_id):
            raise SalesforceUnavailable()
        return case_id

    async def case_number(self, case_id: str) -> str | None:
        if not _ID.fullmatch(case_id):
            raise ValueError("Not a Salesforce id")
        body = await self._api("GET", f"sobjects/Case/{case_id}", params={"fields": "CaseNumber"})
        number = body.get("CaseNumber") if isinstance(body, dict) else None
        return number if isinstance(number, str) else None


class SalesforceConnection:
    """The API's in-memory connection: from settings, replaced by a successful test."""

    def __init__(
        self,
        credentials: Credentials | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._transport = transport
        self.client = (
            SalesforceClient(credentials, transport=transport) if credentials is not None else None
        )
        self.edition: str | None = None

    @classmethod
    def from_settings(
        cls, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> "SalesforceConnection":
        url, client_id = settings.cda_sf_instance_url, settings.cda_sf_client_id
        secret = settings.cda_sf_client_secret
        credentials = None
        if url and client_id and secret and secret.get_secret_value():
            try:
                credentials = Credentials(url, client_id, secret)
            except ValueError:
                log.warning("CDA_SF_INSTANCE_URL is not an https origin; Salesforce is off")
        return cls(credentials, transport=transport)

    def __repr__(self) -> str:
        return f"SalesforceConnection(configured={self.configured}, edition={self.edition!r})"

    @property
    def configured(self) -> bool:
        return self.client is not None

    @property
    def instance_url(self) -> str | None:
        return self.client.credentials.instance_url if self.client is not None else None

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "instance_url": self.instance_url,
            "edition": self.edition,
        }

    async def connect(self, credentials: Credentials) -> str | None:
        """Test new credentials; only on success do they replace the current ones."""
        client = SalesforceClient(credentials, transport=self._transport)
        edition = await client.edition()
        self.client, self.edition = client, edition
        return edition

    def case_url(self, case_id: str) -> str | None:
        if self.instance_url is None or not _ID.fullmatch(case_id):
            return None
        return f"{self.instance_url}/lightning/r/Case/{case_id}/view"


def case_subject(check_id: str, title: str) -> str:
    subject = f"{check_id} · {title}"
    return subject if len(subject) <= SUBJECT_MAX else subject[: SUBJECT_MAX - 1] + "…"


def _location(evidence: dict[str, Any]) -> str:
    filename = str(evidence.get("filename") or "Unknown file")
    parts = [filename]
    if evidence.get("part_label"):
        parts.append(str(evidence["part_label"]))
    name = filename.lower()
    unit = "section" if name.endswith(".docx") else "sheet" if name.endswith(".xlsx") else "page"
    place = f"{unit} {evidence.get('pdf_page')}"
    printed = evidence.get("printed_page")
    if unit == "page" and printed and str(printed) != str(evidence.get("pdf_page")):
        place += f" (printed page {printed})"
    parts.append(place)
    return ", ".join(parts)


def case_description(
    *,
    status: str,
    rationale: str,
    missing_aspects: list[str],
    evidence: list[dict[str, Any]],
    report_link: str,
) -> str:
    """Plain-text Case description: status, rationale, evidence locations and quotes."""
    lines = [f"Status: {_STATUS_LABELS.get(status, status)}", "", "Rationale:", rationale.strip()]
    if missing_aspects:
        lines += ["", "Missing:"] + [f"- {aspect}" for aspect in missing_aspects]
    lines += ["", "Evidence:"]
    if not evidence:
        lines.append("No evidence was cited.")
    for i, item in enumerate(evidence, 1):
        lines.append(f"{i}. {_location(item)}")
        lines.append(f'   Quote: "{str(item.get("quote", "")).strip()}"')
    body = "\n".join(lines)
    tail = f"\n\nReport: {report_link}"
    if len(body) + len(tail) > DESCRIPTION_MAX:
        body = body[: DESCRIPTION_MAX - len(tail) - 1] + "…"
    return body + tail


def case_fields(check_id: str, title: str, account_id: str, description: str) -> dict[str, str]:
    return {
        "Subject": case_subject(check_id, title),
        "Description": description,
        "AccountId": account_id,
        "Origin": ORIGIN,
    }
