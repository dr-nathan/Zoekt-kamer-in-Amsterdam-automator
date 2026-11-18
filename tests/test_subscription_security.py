from __future__ import annotations

import base64
import hashlib
import hmac
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from fb_automator.notifications import (
    NotificationSettings,
    NotificationStore,
    SearchSpec,
    csrf_token,
    is_valid_app_secret,
    sign_action,
    token_hash,
    verify_action,
    verify_csrf,
)
from fb_automator.storage import SCHEMA


SECRET = "synthetic-notification-security-test-secret-32-plus"
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


class SubscriptionSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "synthetic.db"
        self.clock = patch("fb_automator.notifications.utc_now", return_value=NOW)
        self.clock.start()

    def tearDown(self) -> None:
        self.clock.stop()
        self.temporary.cleanup()

    @staticmethod
    def snapshot(store: NotificationStore, subscriber_id: int, channel_id: int) -> tuple:
        return (
            dict(store.connection.execute("SELECT * FROM subscribers WHERE id = ?", (subscriber_id,)).fetchone()),
            dict(store.connection.execute("SELECT * FROM notification_channels WHERE id = ?", (channel_id,)).fetchone()),
            [dict(row) for row in store.list_searches(subscriber_id)],
        )

    def test_public_proposals_preserve_existing_accounts_until_fresh_email_proof(self) -> None:
        with NotificationStore(self.database) as store:
            for index, status in enumerate(("active", "paused", "unsubscribed", "bounced", "pending")):
                with self.subTest(status=status):
                    email = f"owner{index}@example.invalid"
                    search = SearchSpec(city="lausanne", max_rent=1500)
                    channel, original_token = store.create_email_subscription(email, search, "Owner")
                    store.verify_email(original_token)
                    search_id = store.list_searches(channel.subscriber_id)[0]["id"]
                    store.set_search_active(search_id, channel.subscriber_id, False)
                    store.set_channel_status(channel.id, status)
                    before = self.snapshot(store, channel.subscriber_id, channel.id)

                    staged, token = store.create_email_subscription(email, search, "Proposed name")

                    self.assertEqual(staged.id, channel.id)
                    self.assertIsInstance(token, str)
                    self.assertEqual(self.snapshot(store, channel.subscriber_id, channel.id), before)
                    self.assertNotEqual(token, original_token)

                    verified = store.verify_email(token)
                    self.assertEqual(verified.status, "active")
                    self.assertEqual(verified.display_name, "Proposed name")
                    self.assertEqual(store.list_searches(channel.subscriber_id)[0]["active"], 1)
                    self.assertIsNone(store.verify_email(token))

    def test_new_account_search_and_name_wait_for_confirmation(self) -> None:
        with NotificationStore(self.database) as store:
            channel, token = store.create_email_subscription(
                "new@example.invalid", SearchSpec(city="bern"), "Proposed name"
            )
            self.assertEqual(channel.status, "pending")
            self.assertEqual(channel.display_name, "")
            self.assertEqual(store.list_searches(channel.subscriber_id), [])
            self.assertEqual(store.active_channels(), [])
            verified = store.verify_email(token)
            self.assertEqual(verified.display_name, "Proposed name")
            self.assertEqual(store.list_searches(channel.subscriber_id)[0]["city"], "bern")

    def test_failed_delivery_discard_preserves_account_and_other_requests(self) -> None:
        with NotificationStore(self.database) as store:
            channel, first = store.create_email_subscription("owner@example.invalid", SearchSpec(city="bern"), "Owner")
            store.verify_email(first)
            store.set_channel_status(channel.id, "unsubscribed")
            before = self.snapshot(store, channel.subscriber_id, channel.id)
            _, deliverable = store.create_email_subscription("owner@example.invalid", SearchSpec(city="lausanne"), "Delivered")
            _, undelivered = store.create_email_subscription("owner@example.invalid", SearchSpec(city="amsterdam"), "Undelivered")
            store.discard_email_subscription_request(undelivered)
            self.assertEqual(self.snapshot(store, channel.subscriber_id, channel.id), before)
            self.assertIsNone(store.verify_email(undelivered))
            self.assertEqual(store.verify_email(deliverable).display_name, "Delivered")
            self.assertEqual({s["city"] for s in store.list_searches(channel.subscriber_id)}, {"bern", "lausanne"})

    def test_confirmation_invalidates_other_outstanding_proposals(self) -> None:
        with NotificationStore(self.database) as store:
            channel, first = store.create_email_subscription("owner@example.invalid", SearchSpec(city="bern"), "First")
            _, second = store.create_email_subscription("owner@example.invalid", SearchSpec(city="lausanne"), "Second")
            self.assertEqual(store.verify_email(first).display_name, "First")
            self.assertIsNone(store.verify_email(second))
            self.assertEqual(len(store.list_searches(channel.subscriber_id)), 1)

    def test_simultaneous_confirmation_applies_proposal_once(self) -> None:
        with NotificationStore(self.database) as store:
            channel, token = store.create_email_subscription("owner@example.invalid", SearchSpec(city="bern"))
        barrier = Barrier(2)

        def confirm() -> bool:
            with NotificationStore(self.database) as store:
                barrier.wait(timeout=5)
                return store.verify_email(token) is not None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: confirm(), range(2)))
        self.assertEqual(sorted(results), [False, True])
        with NotificationStore(self.database) as store:
            self.assertEqual(len(store.list_searches(channel.subscriber_id)), 1)
            count = store.connection.execute(
                "SELECT COUNT(*) FROM admin_events WHERE event_type = 'subscription_verified'"
            ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_expired_confirmation_preserves_subscription(self) -> None:
        with NotificationStore(self.database) as store:
            channel, first = store.create_email_subscription("owner@example.invalid", SearchSpec(city="bern"), "Owner")
            store.verify_email(first)
            store.set_channel_status(channel.id, "paused")
            before = self.snapshot(store, channel.subscriber_id, channel.id)
            _, pending = store.create_email_subscription("owner@example.invalid", SearchSpec(city="lausanne"), "Expired")
            with patch("fb_automator.notifications.utc_now", return_value=NOW + timedelta(hours=24)):
                self.assertIsNone(store.verify_email(pending))
            self.assertEqual(self.snapshot(store, channel.subscriber_id, channel.id), before)

    def test_additive_migration_and_legacy_pending_links_have_bounded_lifetime(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.executescript(SCHEMA)
        with NotificationStore(self.database) as store:
            for index, age in enumerate((timedelta(hours=1), timedelta(hours=24))):
                with self.subTest(age=age):
                    channel, proposal = store.create_email_subscription(f"legacy{index}@example.invalid", SearchSpec(city="bern"))
                    store.discard_email_subscription_request(proposal)
                    legacy = f"legacy-confirmation-token-{index}"
                    store.connection.execute(
                        "UPDATE notification_channels SET verification_token_hash = ?, updated_at = ? WHERE id = ?",
                        (token_hash(legacy), (NOW - age).isoformat(), channel.id),
                    )
                    store.connection.commit()
                    if age < timedelta(hours=24):
                        self.assertEqual(store.verify_email(legacy).status, "active")
                        self.assertIsNone(store.verify_email(legacy))
                    else:
                        self.assertIsNone(store.verify_email(legacy))
                        self.assertEqual(store.get_channel(channel.id).status, "pending")

    def test_token_and_csrf_verification_fail_closed_for_missing_or_short_secrets(self) -> None:
        settings = NotificationSettings("https://example.invalid", SECRET, "synthetic", "audit@example.invalid", "", "bot", "username", "webhook")
        self.assertTrue(settings.email_enabled)
        self.assertTrue(settings.telegram_enabled)
        for secret in ("", "short", " " * 40):
            with self.subTest(secret_length=len(secret)):
                self.assertFalse(is_valid_app_secret(secret))
                self.assertFalse(replace(settings, app_secret=secret).email_enabled)
                self.assertFalse(replace(settings, app_secret=secret).telegram_enabled)
                payload = b"v1|manage|1"
                signature = hmac.new(secret.encode(), payload, hashlib.sha256).digest()[:18]
                forged = ".".join(base64.urlsafe_b64encode(item).decode().rstrip("=") for item in (payload, signature))
                self.assertIsNone(verify_action(secret, "manage", forged))
                with self.assertRaises(RuntimeError):
                    sign_action(secret, "manage", 1)
                self.assertEqual(csrf_token(secret, "subscribe"), "")
                self.assertFalse(verify_csrf(secret, "subscribe", "a" * 64))

    def test_malformed_tokens_and_non_ascii_csrf_do_not_raise(self) -> None:
        for token in ("", "bad.token", "é.é", "a" * 513, "....", None):
            with self.subTest(token_type=type(token).__name__):
                self.assertIsNone(verify_action(SECRET, "manage", token))
        for csrf in ("é" * 64, "a" * 1000, "", None, "a" * 63):
            self.assertFalse(verify_csrf(SECRET, "subscribe", csrf))
        token = sign_action(SECRET, "manage", 42)
        self.assertEqual(verify_action(SECRET, "manage", token), 42)
        self.assertIsNone(verify_action(SECRET, "unsubscribe", token))
        self.assertTrue(verify_csrf(SECRET, "subscribe", csrf_token(SECRET, "subscribe")))
        self.assertIsNone(verify_action(SECRET, "manage", sign_action(SECRET, "manage", 0)))


if __name__ == "__main__":
    unittest.main()
