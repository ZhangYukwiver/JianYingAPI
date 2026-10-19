from __future__ import annotations

import re

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .errors import ServiceError


_LOCAL_AUTHORITY = re.compile(
    r"(127\.0\.0\.1|localhost|\[::1\])(?::([0-9]{1,5}))?", re.IGNORECASE
)


def _authority(value: str, scheme: str) -> tuple[str, int] | None:
    match = _LOCAL_AUTHORITY.fullmatch(value)
    if match is None:
        return None
    port = int(match[2]) if match[2] is not None else (443 if scheme == "https" else 80)
    if not 1 <= port <= 65535:
        return None
    return match[1].lower(), port


class LocalRequestMiddleware:
    """Restrict browser requests to the local service's own origin.

    Loopback binding alone does not prevent DNS rebinding or cross-site form
    submissions. Local HTTP clients without browser headers remain supported.
    This guard is not authentication against other processes on the same PC.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        hosts = headers.getlist("host")
        scheme = scope.get("scheme", "http")
        authority = _authority(hosts[0], scheme) if len(hosts) == 1 else None
        if authority is None:
            error = ServiceError(
                "HTTP_HOST_REJECTED", "A loopback Host header is required", status=400
            )
        else:
            origins = headers.getlist("origin")
            expected = (scheme, *authority)
            origin = None
            if len(origins) == 1 and "://" in origins[0]:
                origin_scheme, origin_host = origins[0].split("://", 1)
                origin_authority = _authority(origin_host, origin_scheme)
                if origin_authority is not None:
                    origin = (origin_scheme, *origin_authority)
            fetch_sites = headers.getlist("sec-fetch-site")
            if (
                (origins and origin != expected)
                or len(fetch_sites) > 1
                or (fetch_sites and fetch_sites[0].lower() not in {"same-origin", "none"})
            ):
                error = ServiceError(
                    "HTTP_ORIGIN_REJECTED",
                    "Cross-origin browser access is not allowed",
                    status=403,
                )
            else:
                await self.app(scope, receive, send)
                return
        response = JSONResponse(status_code=error.status, content=error.as_dict())
        await response(scope, receive, send)
