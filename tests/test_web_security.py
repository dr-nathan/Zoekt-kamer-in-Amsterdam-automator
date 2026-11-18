from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from fb_automator.notifications import NotificationStore, SearchSpec, csrf_token
from fb_automator.security import MAX_REQUEST_BYTES, SECURITY_HEADERS, RequestLimiter, SecurityMiddleware
from fb_automator.web import create_app


SECRET = "synthetic-web-security-test-app-secret-32-plus"
ADMIN_SECRET = "synthetic-web-security-test-proxy-secret-32-plus"


class WebSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "synthetic.db"
        with NotificationStore(self.database):
            pass
        self.environment = {
            "APP_SECRET": SECRET,
            "ADMIN_PROXY_SECRET": ADMIN_SECRET,
            "PUBLIC_BASE_URL": "https://facebookrooms.nl",
            "RESEND_API_KEY": "synthetic-provider-key",
            "RESEND_FROM": "audit@example.invalid",
            "FB_ALLOWED_HOSTS": "testserver,facebookrooms.nl",
        }
        self.env_patch = patch.dict(os.environ, self.environment, clear=True)
        self.env_patch.start()
        self.send_patch = patch("fb_automator.web.send_resend_email", return_value="synthetic-message-id")
        self.send = self.send_patch.start()
        self.application = create_app(self.database)
        self.client = TestClient(self.application)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.send_patch.stop()
        self.env_patch.stop()
        self.temporary.cleanup()

    @staticmethod
    def form(email: str = "owner@example.invalid") -> dict[str, str]:
        return {
            "csrf": csrf_token(SECRET, "subscribe"), "email": email,
            "city": "lausanne", "max_rent": "1500", "display_name": "Proposed name",
        }

    def seed_owner(self, status: str = "active"):
        with NotificationStore(self.database) as store:
            channel, token = store.create_email_subscription(
                "owner@example.invalid", SearchSpec(city="lausanne", max_rent=1500), "Owner"
            )
            store.verify_email(token)
            store.set_search_active(store.list_searches(channel.subscriber_id)[0]["id"], channel.subscriber_id, False)
            store.set_channel_status(channel.id, status)
            return channel

    def test_admin_guard_limits_requests_before_proxy_password_authentication(self):
        self.assertEqual(self.client.get("/internal/admin-guard").status_code, 404)
        headers = {"X-Chineur-Admin": ADMIN_SECRET}
        for _ in range(10):
            self.assertEqual(self.client.get("/internal/admin-guard", headers=headers).status_code, 204)
        response = self.client.get("/internal/admin-guard", headers=headers)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "60")

    def owner_snapshot(self, channel) -> tuple:
        with NotificationStore(self.database) as store:
            return (
                dict(store.connection.execute("SELECT * FROM subscribers WHERE id = ?", (channel.subscriber_id,)).fetchone()),
                dict(store.connection.execute("SELECT * FROM notification_channels WHERE id = ?", (channel.id,)).fetchone()),
                [dict(search) for search in store.list_searches(channel.subscriber_id)],
            )

    def test_administrator_requires_configured_proxy_secret(self) -> None:
        for headers in ({}, {"x-chineur-admin": "1"}, {"x-chineur-admin": "wrong"}):
            self.assertEqual(self.client.get("/admin", headers=headers).status_code, 404)
        response = self.client.get("/admin", headers={"x-chineur-admin": ADMIN_SECRET})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        for configured in ("", "short"):
            with patch.dict(os.environ, {"ADMIN_PROXY_SECRET": configured}):
                with TestClient(create_app(self.database)) as client:
                    self.assertEqual(client.get("/admin", headers={"x-chineur-admin": configured or "1"}).status_code, 404)

    def test_public_diagnostics_do_not_expose_schema_authorization_or_paths(self) -> None:
        for path in ("/openapi.json", "/docs", "/redoc", "/.well-known/security-verification.txt"):
            self.assertEqual(self.client.get(path).status_code, 404)
        response = self.client.get("/health")
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertNotIn(str(self.database), response.text)
        self.assertNotIn("database", response.json())
        for name, value in SECURITY_HEADERS.items():
            self.assertEqual(response.headers[name], value)

    def test_host_and_origin_controls_allow_expected_requests(self) -> None:
        valid = self.client.post("/subscriptions/email", data=self.form(), headers={"origin": "http://testserver"})
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(self.send.call_count, 1)
        hostile = self.client.post("/subscriptions/email", data=self.form(), headers={"origin": "https://hostile.example.invalid"})
        self.assertEqual(hostile.status_code, 403)
        hostile_fetch = self.client.post("/subscriptions/email", data=self.form(), headers={"origin": "http://testserver", "sec-fetch-site": "cross-site"})
        self.assertEqual(hostile_fetch.status_code, 403)
        self.assertEqual(self.client.get("/health", headers={"host": "hostile.example.invalid"}).status_code, 400)
        self.assertEqual(self.send.call_count, 1)

    def test_declared_request_body_limit_and_invalid_lengths(self) -> None:
        for length, expected in ((str(MAX_REQUEST_BYTES + 1), 413), ("-1", 400), ("unknown", 400), ("9" * 100, 413)):
            with self.subTest(length=length):
                response = self.client.post("/webhooks/telegram", content=b"small", headers={"content-length": length})
                self.assertEqual(response.status_code, expected)
        self.send.assert_not_called()

    def test_streamed_request_body_limit_without_content_length(self) -> None:
        async def exercise(chunks: list[bytes]) -> tuple[int, bool]:
            called = False
            messages = []

            async def endpoint(scope, receive, send):
                nonlocal called
                called = True
                body = await receive()
                await JSONResponse({"bytes": len(body["body"])})(scope, receive, send)

            middleware = SecurityMiddleware(endpoint, RequestLimiter())
            scope = {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": "POST", "scheme": "http", "path": "/webhooks/local-test",
                "raw_path": b"/webhooks/local-test", "query_string": b"",
                "headers": [(b"host", b"testserver"), (b"transfer-encoding", b"chunked")],
                "client": ("127.0.0.1", 1234), "server": ("testserver", 80),
            }
            index = 0

            async def receive():
                nonlocal index
                if index >= len(chunks):
                    return {"type": "http.disconnect"}
                message = {"type": "http.request", "body": chunks[index], "more_body": index < len(chunks) - 1}
                index += 1
                return message

            async def send(message):
                messages.append(message)

            await middleware(scope, receive, send)
            status = next(message["status"] for message in messages if message["type"] == "http.response.start")
            return status, called

        self.assertEqual(asyncio.run(exercise([b"a" * (MAX_REQUEST_BYTES // 2), b"b" * (MAX_REQUEST_BYTES // 2)])), (200, True))
        self.assertEqual(asyncio.run(exercise([b"a" * MAX_REQUEST_BYTES, b"b"])), (413, False))

    def test_malformed_or_excessive_form_fields_return_client_error(self) -> None:
        bodies = [b"&".join(f"field{i}=x".encode() for i in range(101)), b"email=\xff", b"email=%FF"]
        for body in bodies:
            response = self.client.post("/subscriptions/email", content=body, headers={"content-type": "application/x-www-form-urlencoded"})
            self.assertEqual(response.status_code, 400)
        self.send.assert_not_called()

    def test_subscription_request_limit_blocks_sixth_request_without_delivery(self) -> None:
        for _ in range(5):
            self.assertEqual(self.client.post("/subscriptions/email", data={"city": "lausanne"}).status_code, 403)
        limited = self.client.post("/subscriptions/email", data=self.form())
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.headers["retry-after"], "600")
        self.send.assert_not_called()

    def test_active_and_unknown_email_responses_are_uniform_and_defer_changes(self) -> None:
        channel = self.seed_owner()
        before = self.owner_snapshot(channel)
        existing = self.client.post("/subscriptions/email", data=self.form())
        unknown = self.client.post("/subscriptions/email", data=self.form("new@example.invalid"))
        self.assertEqual((existing.status_code, unknown.status_code), (200, 200))
        self.assertEqual(existing.text, unknown.text)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.owner_snapshot(channel), before)
        self.assertNotIn("déjà vérifiée", existing.text)
        self.assertEqual(existing.headers["cache-control"], "no-store")

    def test_failed_email_delivery_preserves_existing_state_and_discards_proposal(self) -> None:
        channel = self.seed_owner("paused")
        before = self.owner_snapshot(channel)
        self.send.side_effect = RuntimeError("synthetic delivery failure")
        response = self.client.post("/subscriptions/email", data=self.form())
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.owner_snapshot(channel), before)
        with NotificationStore(self.database) as store:
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM pending_email_subscriptions").fetchone()[0], 0)

    def test_missing_or_short_app_secret_disables_notifications_and_rejects_forged_tokens(self) -> None:
        channel = self.seed_owner()
        before = self.owner_snapshot(channel)
        for secret in ("", "short"):
            with patch.dict(os.environ, {"APP_SECRET": secret}):
                payload = f"v1|manage|{channel.id}".encode()
                signature = hmac.new(secret.encode(), payload, hashlib.sha256).digest()[:18]
                forged = ".".join(base64.urlsafe_b64encode(value).decode().rstrip("=") for value in (payload, signature))
                with TestClient(create_app(self.database)) as client:
                    self.assertEqual(client.get("/health").status_code, 200)
                    self.assertEqual(client.post("/subscriptions/email", data=self.form()).status_code, 503)
                    self.assertEqual(client.get(f"/subscriptions/manage/{forged}").status_code, 400)
                    self.assertEqual(client.post(f"/subscriptions/manage/{forged}", data={"action": "pause"}).status_code, 400)
        self.assertEqual(self.owner_snapshot(channel), before)
        self.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
