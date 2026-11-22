import asyncio
import ssl
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx

from testing_agent.domain.requirements import HttpMethod, OperationReference

SANDBOX_ORIGIN = "http://sandbox.invalid"


def _validate_headers(headers: Mapping[str, str] | None) -> None:
    """Validates that the provided headers do not contain forbidden headers or control characters."""
    
    forbidden = {
        "host", "content-length", "transfer-encoding", "connection", "proxy-authorization",
    }
    
    for name, value in (headers or {}).items():
        if name.lower() in forbidden:
            raise ValueError(f"The header {name!r} is managed by the HTTP client.")

        if any(ord(char) < 32 or ord(char) == 127 for char in name + value):
            raise ValueError("HTTP headers must not contain control characters.")


class TargetHttpClient:
    """Transport prepared by open_http_client; does not decide business permissions.

    Each instance retains its session and cookies. It should belong to a single
    investigation and identity; it should not be shared between independent runs.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        timeout_seconds: float,
        max_response_bytes: int,
        *, private_asgi: bool = False,
    ) -> None:
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self.private_asgi = private_asgi

    async def request(
        self,
        method: HttpMethod,
        path: str,
        *,
        params: dict[str, str | list[str]] | None = None,
        json_body: Any = None,
        body: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Sends a request and returns a serializable observation.
        
        path is a full server path with resolved path parameters.
        Query parameters are provided separately. body allows sending literal text,
        including invalid JSON or the JSON null value.
        Network errors and timeouts propagate without retrying the operation.  
        """
        operation = OperationReference(method=method, path=path)

        if (
            path.startswith("//")
            or "\\" in path
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
        ):
            raise ValueError("Use an absolute server path, not a URL or network-path reference.")
        
        if method == "CONNECT":
            raise ValueError("HTTP tunneling is not supported.")
        
        if body is not None and json_body is not None:
            raise ValueError("Provide body or json_body, not both.")
        
        _validate_headers(headers)

        # Cambiar unicamente el path conserva esquema, servidor y puerto.
        url = self._client.base_url.copy_with(path=operation.path)

        async with asyncio.timeout(self._timeout_seconds):
            async with self._client.stream(
                operation.method,
                url,
                params=params,
                json=json_body,
                content=body,
                headers=headers,
                follow_redirects=False,
            ) as response:
                content = bytearray()
                truncated = False

                async for chunk in response.aiter_bytes(chunk_size=65536):
                    remaining = self._max_response_bytes - len(content)
                    content.extend(chunk[:remaining])

                    if len(chunk) > remaining:
                        truncated = True
                        break

                encoding = response.encoding or "utf-8"

                return {
                    "method": response.request.method,
                    "url": str(response.request.url),
                    "status_code": response.status_code,
                    "headers": [list(item) for item in response.headers.multi_items()],
                    "body": content.decode(encoding, errors="replace"),
                    "body_encoding": encoding,
                    "body_bytes": len(content),
                    "body_truncated": truncated,
                }


@asynccontextmanager
async def open_http_client(
    base_url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout_seconds: float = 15,
    max_response_bytes: int = 1_048_576,
    ssl_context: ssl.SSLContext | None = None,
    proxy: str | None = None,
    application: Any = None,
) -> AsyncGenerator[TargetHttpClient, None]:
    """Opens and closes an HTTP session with explicit configuration.

    base_url contains only the origin, e.g. http://127.0.0.1:8000.
    The credentials, proxy and CAs are provided from the application.
    timeout_seconds limits both the network wait and the total duration.
    max_response_bytes limits the uncompressed bytes retained from the body.
    """

    if not isinstance(base_url, str) or any(char.isspace() for char in base_url):
        raise ValueError("base_url must be an HTTP(S) origin without whitespace.")

    origin = httpx.URL(base_url)

    if (
        origin.scheme not in {"http", "https"}
        or not origin.host
        or origin.userinfo
        or origin.raw_path != b"/"
        or "#" in base_url
    ):
        raise ValueError("base_url must contain only scheme, host and optional port.")
    
    if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds < float("inf"):
        raise ValueError("timeout_seconds must be positive and finite.")
    
    if type(max_response_bytes) is not int or max_response_bytes < 1:
        raise ValueError("max_response_bytes must be a positive integer.")
    
    if ssl_context is not None and not isinstance(ssl_context, ssl.SSLContext):
        raise ValueError("ssl_context must be an SSLContext instance.")
    
    _validate_headers(headers)

    hooks = {}
    transport = None

    if application is not None:
        if base_url != SANDBOX_ORIGIN or headers or proxy:
            raise ValueError("The private sandbox uses a fixed origin and anonymous identity.")

        transport = httpx.ASGITransport(app=application)

        async def anonymous(request: httpx.Request) -> None:
            request.headers.pop("cookie", None)
            request.headers.pop("authorization", None)

        hooks = {"request": [anonymous]}

    async with httpx.AsyncClient(
        base_url=origin,
        headers=headers,
        timeout=timeout_seconds,
        follow_redirects=False,
        trust_env=False,
        verify=ssl_context if ssl_context is not None else True,
        proxy=proxy,
        transport=transport, event_hooks=hooks,
    ) as client:
        yield TargetHttpClient(client, timeout_seconds, max_response_bytes, private_asgi=application is not None)
