import sqlite3
import tempfile
import unittest
from pathlib import Path

from fb_automator.extractor import extract_database, extract_listing
from fb_automator.listing_models import (
    ApplicantStatus,
    FurnishingStatus,
    GenderRequirement,
    LeaseType,
    ListingKind,
    RegistrationStatus,
    UtilitiesStatus,
)
from fb_automator.models import RawPost
from fb_automator.storage import PostStore


class ExtractorTests(unittest.TestCase):
    def test_extracts_filter_fields_and_particularities(self) -> None:
        listing = extract_listing(
            "post-key",
            """
            Tijdelijk gemeubileerde kamer te huur in De Pijp vanaf 1 oktober.
            De kamer is 11 m2. Huur: € 1.150 per maand all-in. De borg is €1150.
            Inschrijving niet mogelijk. We zoeken een werkende vrouw van 25+.
            Balkon en vaatwasser aanwezig.
            """,
            "2026-09-16T10:00:00+00:00",
        )

        self.assertEqual(listing.listing_kind, ListingKind.OFFER)
        self.assertEqual(listing.monthly_rent, 1150)
        self.assertEqual(listing.room_size_m2, 11)
        self.assertEqual(listing.location_text, "De Pijp")
        self.assertEqual(listing.available_from, "2026-10-01")
        self.assertEqual(listing.lease_type, LeaseType.SUBLET)
        self.assertEqual(listing.registration, RegistrationStatus.NOT_ALLOWED)
        self.assertEqual(listing.utilities, UtilitiesStatus.INCLUDED)
        self.assertEqual(listing.furnishing, FurnishingStatus.FURNISHED)
        self.assertEqual(listing.gender, GenderRequirement.WOMEN)
        self.assertEqual(listing.applicant_status, ApplicantStatus.WORKING)
        self.assertEqual(listing.age_min, 25)
        self.assertIn("No registration", listing.particularities)
        self.assertIn("Temporary/sublet", listing.particularities)
        self.assertIn("Women only", listing.particularities)
        self.assertIn("balcony", listing.amenities)

    def test_recognizes_wanted_post_and_comma_thousands(self) -> None:
        listing = extract_listing(
            "wanted-key",
            "KAMER / APPARTEMENT GEZOCHT IN AMSTERDAM. Max rent €1,245 per month.",
            "2026-09-16T10:00:00+00:00",
        )
        self.assertEqual(listing.listing_kind, ListingKind.WANTED)
        self.assertEqual(listing.monthly_rent, 1245)
        self.assertIn("Wanted, not offered", listing.particularities)

    def test_does_not_treat_roommate_apartment_size_as_room_size(self) -> None:
        listing = extract_listing(
            "size-key",
            "Looking for a roommate for my cozy apartment (76m²). Rent is €1,245.",
            "2026-09-16T10:00:00+00:00",
        )
        self.assertEqual(listing.listing_kind, ListingKind.OFFER)
        self.assertIsNone(listing.room_size_m2)
        self.assertEqual(listing.property_size_m2, 76)

    def test_interprets_half_month_as_fifteenth(self) -> None:
        listing = extract_listing(
            "date-key",
            "Huisgenootje gezocht per half oktober!",
            "2026-09-16T10:00:00+00:00",
        )
        self.assertEqual(listing.available_from, "2026-10-15")

    def test_extracts_database_into_separate_table(self) -> None:
        post = RawPost(
            group_name="Housing",
            group_url="https://www.facebook.com/groups/123/",
            post_id="456",
            post_url="https://www.facebook.com/groups/123/posts/456",
            text="Room available in Amsterdam West, 18 m2, rent €900 p/m.",
            published_label="2 h",
            scraped_at="2026-09-16T10:00:00+00:00",
        )
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "posts.db"
            with PostStore(database) as store:
                store.upsert([post])
            self.assertEqual(extract_database(database), (1, 1))
            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    "SELECT listing_kind, monthly_rent, room_size_m2 FROM listings"
                ).fetchone()
        self.assertEqual(row, ("offer", 900, 18))


if __name__ == "__main__":
    unittest.main()
