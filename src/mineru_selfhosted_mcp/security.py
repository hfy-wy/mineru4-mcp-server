"""Pure-ASGI bearer-token middleware.

Chosen over fastmcp auth-provider classes to avoid fastmcp major-version API
churn; a single ASGI wrapper covers the mounted MCP sub-app and /upload
uniformly. /downloads, /healthz, /ui and / stay open (intranet artifacts and
the read-only board must be fetchable without headers); when no token is
configured everything is open.
"""
from __future__ import annotations

import hmac
from typing import Iterable, Sequence

_TYPE_HTTP = "http"


class BearerTokenMiddleware:
    def __init__(
        self,
        app,
        token: str | None,
        exempt_prefixes: Sequence[str] = ("/downloads", "/healthz", "/ui"),
    ) -> None:
        self.app = app
        self.token = token
        self.exempt_prefixes = tuple(exempt_prefixes)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != _TYPE_HTTP or self.token is None:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/" or any(path.startswith(p) for p in self.exempt_prefixes):
            await self.app(scope, receive, send)
            return
        auth = ""
        for k, v in scope.get("headers", []):
            if k == b"authorization":
                auth = v.decode("latin-1")
                break
        expected = f"Bearer {self.token}"
        if hmac.compare_digest(auth.encode(), expected.encode()):
            await self.app(scope, receive, send)
            return
        body = b'{"detail": "invalid or missing bearer token"}'
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b'Bearer realm="mineru-mcp"'),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })
