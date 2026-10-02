from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fb_automator.image_reviewer import ImageReview, review_database_images
from fb_automator.models import RawPost
from fb_automator.storage import PostStore, dedupe_key


class FakeResponses:
    def __init__(self) -> None:
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_parsed=ImageReview(
                primary_image_position=1,
                image_quality_score=91,
            )
        )


class FakeClient:
    def __init__(self) -> None:
        self.responses = FakeResponses()


class ImageReviewerTests(unittest.TestCase):
    def test_selects_and_caches_cover_image(self) -> None:
        post = RawPost(
            group_name="Lausanne",
            group_url="https://www.facebook.com/groups/123/",
            post_id="456",
            post_url="https://www.facebook.com/groups/123/posts/456",
            text="Chambre meublée au centre de Lausanne.",
            published_label=None,
            scraped_at="2026-09-17T10:00:00+00:00",
        )
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "listings.db"
            key = dedupe_key(post)
            with PostStore(database) as store:
                store.upsert([post])
                store.connection.execute(
                    """
                    INSERT INTO listings (
                        raw_post_key, listing_kind, monthly_rent, utilities,
                        deposit_amount, deposit_months, room_size_m2,
                        property_size_m2, location_text, city, neighborhood,
                        available_from, available_to, lease_type, registration,
                        furnishing, gender, age_min, age_max,
                        language_requirement, internationals, applicant_status,
                        private_bathroom, amenities_json, particularities_json,
                        summary, evidence_json, source_hash,
                        extraction_version, extracted_at
                    ) VALUES (
                        ?, 'offer', 900, 'included', NULL, NULL, NULL, NULL,
                        'Centre, Lausanne', 'Lausanne', 'Centre', NULL, NULL,
                        'unknown', 'allowed', 'furnished', 'any', NULL, NULL,
                        'unknown', 'welcome', 'any', NULL, '[]', '[]',
                        'Chambre meublée au centre de Lausanne.', '[]',
                        'source', 'test', '2026-09-17T10:01:00+00:00'
                    )
                    """,
                    (key,),
                )
                for position in range(2):
                    relative = Path("images") / key / f"{position}.jpg"
                    target = database.parent / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(f"image-{position}".encode())
                    store.upsert_image(
                        raw_post_key=key,
                        position=position,
                        source_url=f"https://example.com/{position}.jpg",
                        local_path=relative.as_posix(),
                        content_type="image/jpeg",
                        observed_at=post.scraped_at,
                    )
                store.connection.commit()

            self.assertEqual(
                review_database_images(
                    database,
                    model="vision-test",
                    workers=1,
                    client=client,
                ),
                (1, 0, 0, 1),
            )
            self.assertEqual(
                review_database_images(
                    database,
                    model="vision-test",
                    workers=1,
                    client=client,
                ),
                (0, 1, 0, 1),
            )
            with PostStore(database) as store:
                row = store.connection.execute(
                    """
                    SELECT primary_image_position, image_quality_score,
                           image_review_version
                    FROM listings WHERE raw_post_key = ?
                    """,
                    (key,),
                ).fetchone()

        self.assertEqual(row, (1, 91, "image-v1:vision-test"))
        self.assertEqual(len(client.responses.calls), 1)
        content = client.responses.calls[0]["input"][1]["content"]
        self.assertEqual(
            [item["detail"] for item in content if item["type"] == "input_image"],
            ["low", "low"],
        )


if __name__ == "__main__":
    unittest.main()
