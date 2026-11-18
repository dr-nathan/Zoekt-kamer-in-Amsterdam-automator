"""Small, bounded request controls for the single-process web service."""
from __future__ import annotations

import asyncio
import threading
import time
from collections import OrderedDict, deque
from typing import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fb_automator.visitor_tracking import client_ip

MAX_REQUEST_BYTES = 64 * 1024
SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self' https://t.me; upgrade-insecure-requests"
    ),
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "strict-transport-security": "max-age=31536000",
}


class RequestLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic, max_entries: int = 4096):
        self.clock = clock
        self.max_entries = max_entries
        self.entries: OrderedDict[tuple[str, str], deque[float]] = OrderedDict()
        self.lock = threading.Lock()

    def allow(self, bucket: str, identity: str, *, limit: int, window: int) -> bool:
        now = self.clock()
        key = (bucket, identity)
        with self.lock:
            events = self.entries.pop(key, deque())
            while events and events[0] <= now - window:
                events.popleft()
            permitted = len(events) < limit
            if permitted:
                events.append(now)
            self.entries[key] = events
            while len(self.entries) > self.max_entries:
                self.entries.popitem(last=False)
            return permitted


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, limiter: RequestLimiter):
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = scope.get("path", "")

        async def secured_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {k.lower() for k, _ in headers}
                for name, value in SECURITY_HEADERS.items():
                    if name.encode() not in existing:
                        headers.append((name.encode(), value.encode()))
                if path.startswith(("/admin", "/internal/", "/subscriptions", "/unsubscribe", "/webhooks")):
                    headers = [(k, v) for k, v in headers if k.lower() != b"cache-control"]
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        async def reject(status: int, detail: str, headers: dict[str, str] | None = None) -> None:
            await JSONResponse({"detail": detail}, status_code=status, headers=headers)(scope, receive, secured_send)

        if request.method not in {"GET", "HEAD", "POST", "OPTIONS"}:
            await reject(405, "Method not allowed")
            return
        identity = client_ip(request) or (request.client.host if request.client else "unknown")
        is_asset = path.startswith(("/static/", "/media/"))
        if not self.limiter.allow("assets" if is_asset else "requests", identity,
                                  limit=600 if is_asset else 120, window=60):
            await reject(429, "Too many requests", {"Retry-After": "60"})
            return
        if request.method == "POST" and path in {"/subscriptions/email", "/subscriptions/telegram"}:
            if not self.limiter.allow("subscribe", identity, limit=5, window=600):
                await reject(429, "Too many subscription requests", {"Retry-After": "600"})
                return
        if request.method == "POST" and not path.startswith("/webhooks/"):
            origin = request.headers.get("origin", "")
            if origin:
                from urllib.parse import urlsplit
                try:
                    parsed = urlsplit(origin)
                    expected = urlsplit(f"{scope.get('scheme', 'http')}://{request.headers.get('host', '')}")
                    def origin_key(value):
                        port = value.port if value.port is not None else (443 if value.scheme == "https" else 80)
                        return value.scheme, value.hostname, port
                    if (
                        parsed.scheme not in {"http", "https"}
                        or parsed.username is not None or parsed.password is not None
                        or parsed.path or parsed.query or parsed.fragment
                        or origin_key(parsed) != origin_key(expected)
                    ):
                        await reject(403, "Invalid request origin")
                        return
                except ValueError:
                    await reject(403, "Invalid request origin")
                    return
            if request.headers.get("sec-fetch-site") == "cross-site":
                await reject(403, "Invalid request origin")
                return
        content_length = request.headers.get("content-length")
        if content_length is not None:
            if not content_length.isascii() or not content_length.isdecimal():
                await reject(400, "Invalid content length")
                return
            if len(content_length) > 8 or int(content_length) > MAX_REQUEST_BYTES:
                await reject(413, "Request body too large")
                return
        # Bound streamed/chunked bodies too, without relying on Content-Length.
        if request.method == "POST" or content_length not in (None, "0") or request.headers.get("transfer-encoding"):
            body_buffer = bytearray()
            size = 0
            try:
                async with asyncio.timeout(10):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        if message["type"] != "http.request":
                            await reject(400, "Invalid request body")
                            return
                        chunk = message.get("body", b"")
                        size += len(chunk)
                        if size > MAX_REQUEST_BYTES:
                            await reject(413, "Request body too large")
                            return
                        if chunk:
                            body_buffer.extend(chunk)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                await reject(408, "Request body timed out")
                return
            body = bytes(body_buffer)
            delivered = False

            async def bounded_receive() -> Message:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await self.app(scope, bounded_receive, secured_send)
        else:
            await self.app(scope, receive, secured_send)
