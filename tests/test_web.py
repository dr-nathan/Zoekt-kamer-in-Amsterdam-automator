from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from fb_automator.storage import SCHEMA
from fb_automator.web import ListingRepository, ListingSearch, _optional_int, create_app


class ListingRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "listings.db"
        connection = sqlite3.connect(self.database)
        connection.executescript(SCHEMA)
        connection.execute(
            """
            INSERT INTO raw_posts (
                dedupe_key, group_name, group_url, post_id, post_url, text,
                published_label, reaction_count, comment_count, first_seen_at,
                last_seen_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "abc123", "Amsterdam rooms", "https://facebook.com/groups/example",
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
                dutch_requirement, internationals, applicant_status,
                private_bathroom, amenities_json, particularities_json, summary,
                evidence_json, source_hash, extraction_version, extracted_at
            ) VALUES (
                ?, 'offer', 850, 'included', NULL, NULL, 16, NULL,
                'Oosterpark, Amsterdam', 'Amsterdam', 'Oost', '2026-10-01', NULL,
                'indefinite', 'allowed', 'furnished', 'any', NULL, NULL,
                'none', 'welcome', 'any', 0, '["balcony"]',
                '["Registration possible"]', 'Sunny room near Oosterpark.', '[]',
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
        results = ListingRepository(self.database).search(
            ListingSearch(area="oost", max_rent=900, min_size=12, registration="allowed")
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].rent_label, "€850 / month")
        self.assertEqual(results[0].size_label, "16 m² room")
        self.assertEqual(results[0].engagement_label, "12 reactions · 4 comments")
        self.assertEqual(results[0].image_url, "/media/abc123/0-room.jpg")

    def test_excluding_filter_returns_no_rooms(self) -> None:
        results = ListingRepository(self.database).search(ListingSearch(max_rent=700))
        self.assertEqual(results, [])

    def test_app_exposes_home_health_and_static_routes(self) -> None:
        paths = {getattr(route, "path", None) for route in create_app(self.database).routes}
        self.assertTrue({"/", "/health", "/static"}.issubset(paths))

    def test_empty_number_fields_are_treated_as_unset(self) -> None:
        self.assertIsNone(_optional_int("", maximum=10_000))
        self.assertEqual(_optional_int("850", maximum=10_000), 850)


if __name__ == "__main__":
    unittest.main()
