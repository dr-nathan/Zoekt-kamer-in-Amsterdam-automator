from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from fb_automator.visitor_tracking import browser_label, record_page_view


class VisitorTrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "listings.db"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_records_and_geolocates_a_public_ip(self) -> None:
        record_page_view(
            self.database,
            ip_address="8.8.8.8",
            path="/ville/bern?sort=newest",
            referrer="https://example.com/",
            user_agent="Mozilla/5.0 (Macintosh) Chrome/123.0 Safari/537.36",
            now=datetime(2026, 10, 3, 12, 0, tzinfo=UTC),
            geo_lookup=lambda _: {
                "city": "Zurich",
                "region": "Zurich",
                "country": "Switzerland",
                "country_code": "CH",
                "provider": "Example ISP",
            },
        )

        with sqlite3.connect(self.database) as connection:
            view = connection.execute(
                "SELECT ip_address, path, city, country FROM page_views"
            ).fetchone()
            geo = connection.execute(
                "SELECT provider FROM visitor_geo WHERE ip_address = '8.8.8.8'"
            ).fetchone()
        self.assertEqual(
            view,
            ("8.8.8.8", "/ville/bern?sort=newest", "Zurich", "Switzerland"),
        )
        self.assertEqual(geo, ("Example ISP",))

    def test_keeps_old_visits_and_reuses_cached_geolocation(self) -> None:
        calls = 0

        def lookup(_: str) -> dict[str, str]:
            nonlocal calls
            calls += 1
            return {"city": "Bern", "country": "Switzerland"}

        record_page_view(
            self.database,
            ip_address="1.1.1.1",
            path="/",
            now=datetime(2020, 1, 1, tzinfo=UTC),
            geo_lookup=lookup,
        )
        record_page_view(
            self.database,
            ip_address="1.1.1.1",
            path="/ville/lausanne",
            now=datetime(2020, 1, 2, tzinfo=UTC),
            geo_lookup=lookup,
        )

        with sqlite3.connect(self.database) as connection:
            count = connection.execute("SELECT COUNT(*) FROM page_views").fetchone()[0]
        self.assertEqual(count, 2)
        self.assertEqual(calls, 1)

    def test_browser_label_is_compact(self) -> None:
        self.assertEqual(
            browser_label("Mozilla/5.0 (iPhone) Version/18.0 Mobile Safari/604.1"),
            "Safari · iOS",
        )


if __name__ == "__main__":
    unittest.main()
