"""The HTTP seam of the sync client.

`SyncTransport` is the one interface the client talks to: one request in,
one `HttpResponse` out, or a `TransportError` when nothing usable came back
(connection refused, timeout, TLS failure, a response cut off). Tests inject
a fake server either through `HttpxTransport(transport=httpx.MockTransport)`
(the real httpx code path) or by implementing the protocol directly.

TLS (`api-contract.md` section 1): HTTPS only, TLS 1.2 or newer, certificates
verified (optionally against a site CA bundle). Plain `http://` is refused
unless `allow_insecure=True`, which exists for tests and local development.
"""

import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

import httpx

DEFAULT_TIMEOUT_S: Final = 10.0
"""Per-request timeout. The sync loop runs in its own thread, so a slow
server only delays sync, never a scan."""


class TransportError(Exception):
    """No usable response: the request may or may not have reached the server."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """A response as the client sees it."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    """Header names are lower-case."""
    content: bytes = field(default=b"", repr=False)

    @property
    def ok(self) -> bool:
        """2xx."""
        return 200 <= self.status < 300


class SyncTransport(Protocol):
    """Sends one request to the device API."""

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        content: bytes | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> HttpResponse:
        """Send it; raise `TransportError` if no complete response arrived."""
        ...

    def close(self) -> None:
        """Release connections."""
        ...


def tls_context(ca_bundle: Path | None = None) -> ssl.SSLContext:
    """A verifying client context that refuses anything older than TLS 1.2."""
    context = ssl.create_default_context(cafile=str(ca_bundle) if ca_bundle else None)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class HttpxTransport:
    """`SyncTransport` over an `httpx.Client`. Thread-safe (httpx clients are)."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        ca_bundle: Path | None = None,
        allow_insecure: bool = False,
    ) -> None:
        url = httpx.URL(base_url)
        if url.scheme != "https" and not allow_insecure:
            raise ValueError("the server URL must use https")
        self._client = httpx.Client(
            base_url=url,
            transport=transport,
            verify=tls_context(ca_bundle),
            follow_redirects=False,
            trust_env=False,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        content: bytes | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> HttpResponse:
        """Send one request (see `SyncTransport.request`)."""
        try:
            response = self._client.request(
                method, path, headers=dict(headers), content=content, timeout=timeout_s
            )
            body = response.read()
        except httpx.HTTPError as exc:
            # Type only: messages can carry URLs with query strings.
            raise TransportError(type(exc).__name__) from exc
        return HttpResponse(
            status=response.status_code,
            headers={k.lower(): v for k, v in response.headers.items()},
            content=body,
        )

    def close(self) -> None:
        """Close the connection pool."""
        self._client.close()
