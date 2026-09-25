from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from fb_automator.digest import send_daily_digests
from fb_automator.notifications import (
    NotificationSettings,
    NotificationStore,
    SearchSpec,
    csrf_token,
    sign_action,
    verify_action,
    verify_csrf,
)
from fb_automator.storage import SCHEMA
from fb_automator.web import create_app


class NotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "listings.db"
        connection = sqlite3.connect(self.database)
        connection.executescript(SCHEMA)
        connection.execute(
            """
            INSERT INTO raw_posts (
                dedupe_key, source_city, group_name, group_url, post_id, post_url,
                text, published_label, first_seen_at, last_seen_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "listing-1",
                "lausanne",
                "Lausanne housing",
                "https://facebook.com/groups/example",
                "42",
                "https://facebook.com/groups/example/posts/42",
                "Studio disponible",
                "Friday, September 25, 2026 at 10:00 AM",
                "2026-09-25T08:00:00+00:00",
                "2026-09-25T08:00:00+00:00",
                "{}",
            ),
        )
        connection.execute(
            """
            INSERT INTO listings (
                raw_post_key, listing_kind, monthly_rent, currency, utilities,
                deposit_amount, deposit_months, room_size_m2, property_size_m2,
                location_text, city, neighborhood, available_from, available_to,
                lease_type, registration, furnishing, gender, age_min, age_max,
                language_requirement, internationals, applicant_status,
                private_bathroom, amenities_json, particularities_json, summary,
                evidence_json, source_hash, extraction_version, extracted_at
            ) VALUES (
                ?, 'offer', 1200, 'CHF', 'included', NULL, NULL, NULL, 30,
                'Lausanne 1004', 'Lausanne', 'Centre', '2026-11-01', NULL,
                'indefinite', 'allowed', 'unfurnished', 'any', NULL, NULL,
                'none', 'welcome', 'any', NULL, '[]', '[]',
                'Studio de 30 m² à Lausanne.', '[]', 'hash-1', 'test', ?
            )
            """,
            ("listing-1", "2026-09-25T08:01:00+00:00"),
        )
        connection.commit()
        connection.close()
        self.settings = NotificationSettings(
            base_url="https://facebookrooms.nl",
            app_secret="test-secret-with-enough-entropy",
            resend_api_key="re_test",
            resend_from="alerts@facebookrooms.nl",
            resend_webhook_secret="whsec_test",
            telegram_bot_token="123:test",
            telegram_bot_username="chineur_test_bot",
            telegram_webhook_secret="telegram-secret",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_email_verification_management_and_signed_links(self) -> None:
        search = SearchSpec(city="lausanne", max_rent=1500)
        with NotificationStore(self.database) as store:
            channel, token = store.create_email_subscription(
                "Alex@example.com", search, "Alex"
            )
            self.assertEqual(channel.status, "pending")
            self.assertIsNotNone(token)
            verified = store.verify_email(str(token))
            self.assertIsNotNone(verified)
            self.assertEqual(verified.status, "active")
            self.assertEqual(len(store.list_searches(channel.subscriber_id)), 1)

            store.set_channel_status(channel.id, "paused")
            self.assertEqual(store.get_channel(channel.id).status, "paused")

        manage = sign_action(self.settings.app_secret, "manage", channel.id)
        self.assertEqual(
            verify_action(self.settings.app_secret, "manage", manage), channel.id
        )
        self.assertIsNone(
            verify_action(self.settings.app_secret, "unsubscribe", manage)
        )
        csrf = csrf_token(self.settings.app_secret, "subscribe")
        self.assertTrue(verify_csrf(self.settings.app_secret, "subscribe", csrf))

    def test_telegram_verification_requires_start_token(self) -> None:
        with NotificationStore(self.database) as store:
            channel, token = store.create_telegram_subscription(
                SearchSpec(city="amsterdam"), "Sam"
            )
            verified = store.verify_telegram(token, "12345", "@sam")
            self.assertEqual(verified.destination, "12345")
            self.assertEqual(verified.status, "active")
            self.assertIsNone(store.verify_telegram(token, "999", "@other"))

    def test_telegram_reuses_chat_and_merges_additional_filters(self) -> None:
        with NotificationStore(self.database) as store:
            first, first_token = store.create_telegram_subscription(
                SearchSpec(city="amsterdam"), "Sam"
            )
            store.verify_telegram(first_token, "12345", "@sam")
            _, second_token = store.create_telegram_subscription(
                SearchSpec(city="lausanne", max_rent=1800), "Sam"
            )

            merged = store.verify_telegram(second_token, "12345", "@sam")

            self.assertIsNotNone(merged)
            self.assertEqual(merged.id, first.id)
            self.assertEqual(merged.status, "active")
            self.assertEqual(len(store.list_searches(merged.subscriber_id)), 2)
            telegram_channels = store.connection.execute(
                "SELECT COUNT(*) FROM notification_channels WHERE channel_type = 'telegram'"
            ).fetchone()[0]
            self.assertEqual(telegram_channels, 1)

    def test_daily_digest_is_personalized_and_idempotent(self) -> None:
        with NotificationStore(self.database) as store:
            channel, token = store.create_email_subscription(
                "alex@example.com", SearchSpec(city="lausanne", max_rent=1500), "Alex"
            )
            store.verify_email(str(token))
            store.connection.execute(
                "UPDATE notification_channels SET verified_at = ? WHERE id = ?",
                ("2026-09-25T07:00:00+00:00", channel.id),
            )
            store.connection.commit()

        sent: list[dict[str, object]] = []

        def fake_email_sender(*args: object, **kwargs: object) -> str:
            sent.append(kwargs)
            return "email-123"

        now = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)
        first = send_daily_digests(
            self.database,
            settings=self.settings,
            now=now,
            email_sender=fake_email_sender,
        )
        second = send_daily_digests(
            self.database,
            settings=self.settings,
            now=now,
            email_sender=fake_email_sender,
        )

        self.assertEqual(first.sent, 1)
        self.assertEqual(second.sent, 0)
        self.assertEqual(second.skipped, 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("CHF 1’200 / mois", str(sent[0]["text_body"]))
        self.assertIn("Se désabonner", str(sent[0]["text_body"]))

    def test_web_subscription_confirmation_unsubscribe_and_admin_boundary(self) -> None:
        environment = {
            "APP_SECRET": self.settings.app_secret,
            "PUBLIC_BASE_URL": self.settings.base_url,
            "RESEND_API_KEY": self.settings.resend_api_key,
            "RESEND_FROM": self.settings.resend_from,
        }
        sent: list[dict[str, object]] = []

        def fake_send(*args: object, **kwargs: object) -> str:
            sent.append(kwargs)
            return "verification-1"

        with patch.dict("os.environ", environment, clear=False), patch(
            "fb_automator.web.send_resend_email", side_effect=fake_send
        ):
            with TestClient(create_app(self.database)) as client:
                page = client.get("/ville/lausanne")
                self.assertIn("Recevoir ces annonces chaque matin", page.text)
                response = client.post(
                    "/subscriptions/email",
                    data={
                        "csrf": csrf_token(self.settings.app_secret, "subscribe"),
                        "city": "lausanne",
                        "area": "Centre",
                        "max_rent": "1500",
                        "min_size": "",
                        "registration": "any",
                        "particularity": "",
                        "display_name": "Alex",
                        "email": "alex@example.com",
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn("Vérifiez votre boîte mail", response.text)
                verification_url = str(sent[0]["text_body"]).splitlines()[-1]
                verification_path = verification_url.removeprefix(self.settings.base_url)
                confirmed = client.get(verification_path)
                self.assertIn("Alertes activées", confirmed.text)

                with NotificationStore(self.database) as store:
                    channel = store.channel_by_destination("email", "alex@example.com")
                manage_token = sign_action(self.settings.app_secret, "manage", channel.id)
                unsubscribe_token = sign_action(
                    self.settings.app_secret, "unsubscribe", channel.id
                )
                self.assertEqual(
                    client.get(f"/subscriptions/manage/{manage_token}").status_code,
                    200,
                )
                unsubscribed = client.post(f"/unsubscribe/{unsubscribe_token}")
                self.assertIn("Alertes arrêtées", unsubscribed.text)

                self.assertEqual(client.get("/admin").status_code, 404)
                admin = client.get("/admin", headers={"x-chineur-admin": "1"})
                self.assertEqual(admin.status_code, 200)
                self.assertIn("Tableau de bord", admin.text)


if __name__ == "__main__":
    unittest.main()
