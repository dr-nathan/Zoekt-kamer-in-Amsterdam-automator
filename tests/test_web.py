from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from fb_automator.storage import SCHEMA
from fb_automator.web import ListingRepository, ListingSearch, _optional_int, create_app


class ListingRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "listings.db"
        self.now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        connection = sqlite3.connect(self.database)
        connection.executescript(SCHEMA)
        connection.execute(
            """
            INSERT INTO raw_posts (
                dedupe_key, source_city, group_name, group_url, post_id, post_url, text,
                published_label, reaction_count, comment_count, first_seen_at,
                last_seen_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "abc123", "lausanne", "Logements Lausanne", "https://facebook.com/groups/example",
                "42", "https://facebook.com/groups/example/posts/42", "room",
                "Today", 12, 4, "2026-09-17T09:00:00+00:00",
                "2026-09-17T10:00:00+00:00", "{}",
            ),
        )
        connection.execute(
            """
            INSERT INTO listings (
                raw_post_key, listing_kind, monthly_rent, utilities,
                deposit_amount, deposit_months, room_size_m2, property_size_m2,
                location_text, city, neighborhood, available_from, available_to,
                lease_type, registration, furnishing, gender, age_min, age_max,
                language_requirement, internationals, applicant_status,
                private_bathroom, amenities_json, particularities_json, summary,
                evidence_json, source_hash, extraction_version, extracted_at
            ) VALUES (
                ?, 'offer', 850, 'included', NULL, NULL, 16, NULL,
                'Sous-Gare, Lausanne', 'Lausanne', 'Sous-Gare / Ouchy', '2026-10-01', NULL,
                'indefinite', 'allowed', 'furnished', 'any', NULL, NULL,
                'none', 'welcome', 'any', 0, '["balcon"]',
                '["Domiciliation possible"]', 'Chambre lumineuse à Sous-Gare.', '[]',
                'hash', 'test', '2026-09-17T10:01:00+00:00'
            )
            """,
            ("abc123",),
        )
        image_path = self.database.parent / "images" / "abc123" / "0-room.jpg"
        image_path.parent.mkdir(parents=True)
        image_path.write_bytes(b"fake jpeg")
        connection.execute(
            """
            INSERT INTO post_images (
                raw_post_key, position, source_url, local_path, content_type,
                first_seen_at, last_seen_at
            ) VALUES (?, 0, ?, ?, 'image/jpeg', ?, ?)
            """,
            (
                "abc123",
                "https://scontent.example/room.jpg",
                "images/abc123/0-room.jpg",
                "2026-09-17T10:00:00+00:00",
                "2026-09-17T10:00:00+00:00",
            ),
        )
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_filters_and_formats_room(self) -> None:
        results = ListingRepository(self.database, now=self.now).search(
            ListingSearch(
                area="Sous-Gare / Ouchy",
                max_rent=900,
                min_size=12,
                registration="allowed",
            )
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].rent_label, "CHF 850 / mois")
        self.assertEqual(results[0].size_label, "Chambre de 16 m²")
        self.assertEqual(results[0].engagement_label, "12 réactions · 4 commentaires")
        self.assertEqual(results[0].image_url, "/media/abc123/0-room.jpg")

    def test_excluding_filter_returns_no_rooms(self) -> None:
        results = ListingRepository(self.database, now=self.now).search(
            ListingSearch(max_rent=700)
        )
        self.assertEqual(results, [])

    def test_exact_and_near_duplicate_cross_posts_are_shown_once(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.execute(
            """
            INSERT INTO raw_posts (
                dedupe_key, source_city, group_name, group_url, post_id, post_url, text,
                published_label, reaction_count, comment_count, first_seen_at,
                last_seen_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "duplicate", "lausanne", "Second Lausanne group",
                "https://facebook.com/groups/second", "84",
                "https://facebook.com/groups/second/posts/84", "same cross-post",
                "Today", 15, 6, "2026-09-17T09:00:00+00:00",
                "2026-09-17T11:00:00+00:00", "{}",
            ),
        )
        connection.execute(
            """
            INSERT INTO listings (
                raw_post_key, listing_kind, monthly_rent, utilities,
                deposit_amount, deposit_months, room_size_m2, property_size_m2,
                location_text, city, neighborhood, available_from, available_to,
                lease_type, registration, furnishing, gender, age_min, age_max,
                language_requirement, internationals, applicant_status,
                private_bathroom, amenities_json, particularities_json, summary,
                evidence_json, source_hash, extraction_version, extracted_at
            )
            SELECT ?, listing_kind, monthly_rent, utilities,
                   deposit_amount, deposit_months, room_size_m2, property_size_m2,
                   location_text, city, neighborhood, available_from, available_to,
                   lease_type, registration, furnishing, gender, age_min, age_max,
                   language_requirement, internationals, applicant_status,
                   private_bathroom, amenities_json, particularities_json, summary,
                   evidence_json, source_hash, extraction_version, extracted_at
            FROM listings WHERE raw_post_key = ?
            """,
            ("duplicate", "abc123"),
        )
        connection.commit()
        connection.close()

        results = ListingRepository(self.database, now=self.now).search(ListingSearch())
        self.assertEqual(len(results), 1)

        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                """
                UPDATE listings
                SET source_hash = ?, summary = ?
                WHERE raw_post_key = ?
                """,
                (
                    "different-hash",
                    "Chambre lumineuse à Sous-Gare!",
                    "duplicate",
                ),
            )
            connection.commit()
        finally:
            connection.close()
        results = ListingRepository(self.database, now=self.now).search(ListingSearch())
        self.assertEqual(len(results), 1)

    def test_app_exposes_home_health_and_static_routes(self) -> None:
        paths = {
            getattr(route, "path", None)
            for route in create_app(self.database, now=self.now).routes
        }
        self.assertTrue(
            {"/", "/ville/{city_slug}", "/health", "/static"}.issubset(paths)
        )

    def test_city_picker_and_city_pages_render(self) -> None:
        with TestClient(create_app(self.database, now=self.now)) as client:
            picker = client.get("/")
            self.assertEqual(picker.status_code, 200)
            self.assertIn('/ville/amsterdam', picker.text)
            self.assertIn('/ville/lausanne', picker.text)
            self.assertIn("1 annonce récente", picker.text)

            lausanne = client.get("/ville/lausanne")
            self.assertEqual(lausanne.status_code, 200)
            self.assertIn("CHF 850 / mois", lausanne.text)

            amsterdam = client.get("/ville/amsterdam")
            self.assertEqual(amsterdam.status_code, 200)
            self.assertIn("EUR", amsterdam.text)
            self.assertNotIn("CHF 850 / mois", amsterdam.text)

    def test_outside_city_uses_specific_municipality_for_display(self) -> None:
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                """
                UPDATE listings
                SET city = ?, neighborhood = ?, location_text = ?
                WHERE raw_post_key = ?
                """,
                ("Pully", "Hors Lausanne", "Pully, près de la gare", "abc123"),
            )
            connection.commit()
        finally:
            connection.close()

        results = ListingRepository(self.database, now=self.now).search(
            ListingSearch(city="lausanne")
        )

        self.assertEqual(results[0].location, "Pully")

    def test_city_filter_and_two_week_cutoff(self) -> None:
        repository = ListingRepository(self.database, now=self.now)
        self.assertEqual(len(repository.search(ListingSearch(city="lausanne"))), 1)
        self.assertEqual(repository.search(ListingSearch(city="amsterdam")), [])

        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE raw_posts SET first_seen_at = ? WHERE dedupe_key = ?",
                ("2026-09-09T11:59:59+00:00", "abc123"),
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(repository.search(ListingSearch(city="lausanne")), [])

    def test_actual_facebook_date_also_enforces_two_week_cutoff(self) -> None:
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                """
                UPDATE raw_posts
                SET first_seen_at = ?, published_label = ?
                WHERE dedupe_key = ?
                """,
                (
                    "2026-09-24T10:00:00+00:00",
                    "Wednesday, September 9, 2026 at 9:35 PM",
                    "abc123",
                ),
            )
            connection.commit()
        finally:
            connection.close()

        results = ListingRepository(self.database, now=self.now).search(
            ListingSearch(city="lausanne")
        )
        self.assertEqual(results, [])

    def test_property_area_and_summary_currency_are_displayed_consistently(self) -> None:
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                """
                UPDATE listings
                SET room_size_m2 = NULL, property_size_m2 = 45,
                    currency = 'EUR', summary = 'Appartement à 1 400 CHF.'
                WHERE raw_post_key = ?
                """,
                ("abc123",),
            )
            connection.commit()
        finally:
            connection.close()

        results = ListingRepository(self.database, now=self.now).search(
            ListingSearch(city="lausanne", min_size=40)
        )
        self.assertEqual(results[0].size_label, "Logement de 45 m²")
        self.assertEqual(results[0].rent_label, "CHF 850 / mois")

    def test_empty_number_fields_are_treated_as_unset(self) -> None:
        self.assertIsNone(_optional_int("", maximum=10_000))
        self.assertEqual(_optional_int("850", maximum=10_000), 850)


if __name__ == "__main__":
    unittest.main()
