import tempfile
import unittest
from pathlib import Path

from fb_automator.models import RawPost
from fb_automator.storage import PostStore, dedupe_key


class PostStoreTests(unittest.TestCase):
    def test_upsert_deduplicates_post(self) -> None:
        first = RawPost(
            group_name="Housing",
            group_url="https://www.facebook.com/groups/123/",
            post_id="456",
            post_url="https://www.facebook.com/groups/123/posts/456",
            text="A room is available",
            published_label="2 h",
            scraped_at="2026-09-16T10:00:00+00:00",
            reaction_count=4,
            comment_count=2,
            source_city="lausanne",
            embedded_listing_text="CHF1,300 · Lausanne, VD",
        )
        second = RawPost(
            group_name="Housing",
            group_url=first.group_url,
            post_id=first.post_id,
            post_url=first.post_url,
            text="A room is available (edited)",
            published_label="3 h",
            scraped_at="2026-09-16T11:00:00+00:00",
            reaction_count=9,
            comment_count=5,
            source_city="lausanne",
        )
        with tempfile.TemporaryDirectory() as directory:
            with PostStore(Path(directory) / "posts.db") as store:
                self.assertEqual(store.upsert([first])[:2], (1, 0))
                self.assertEqual(store.upsert([second])[:2], (0, 1))
                self.assertEqual(store.count(), 1)
                row = store.connection.execute(
                    """SELECT text, embedded_listing_text, first_seen_at, last_seen_at,
                              reaction_count, comment_count
                       FROM raw_posts"""
                ).fetchone()
                snapshots = store.connection.execute(
                    """SELECT observed_at, reaction_count, comment_count
                       FROM engagement_snapshots ORDER BY observed_at"""
                ).fetchall()
                key = store.connection.execute(
                    "SELECT dedupe_key FROM raw_posts"
                ).fetchone()[0]
                store.upsert_image(
                    raw_post_key=key,
                    position=0,
                    source_url="https://scontent.example/room.jpg",
                    local_path=f"images/{key}/0-room.jpg",
                    content_type="image/jpeg",
                    observed_at=second.scraped_at,
                )
                image = store.connection.execute(
                    "SELECT source_url, local_path, content_type FROM post_images"
                ).fetchone()
        self.assertEqual(
            row,
            (
                "A room is available (edited)",
                "CHF1,300 · Lausanne, VD",
                "2026-09-16T10:00:00+00:00",
                "2026-09-16T11:00:00+00:00",
                9,
                5,
            ),
        )
        self.assertEqual(
            snapshots,
            [
                ("2026-09-16T10:00:00+00:00", 4, 2),
                ("2026-09-16T11:00:00+00:00", 9, 5),
            ],
        )
        self.assertEqual(
            image,
            (
                "https://scontent.example/room.jpg",
                f"images/{key}/0-room.jpg",
                "image/jpeg",
            ),
        )

    def test_upsert_merges_exact_cross_post_content(self) -> None:
        first = RawPost(
            group_name="Group one",
            group_url="https://www.facebook.com/groups/1/",
            post_id="101",
            post_url="https://www.facebook.com/groups/1/posts/101",
            text="Chambre à Lausanne, CHF 900.",
            published_label=None,
            scraped_at="2026-09-17T10:00:00+00:00",
            source_city="lausanne",
        )
        cross_post = RawPost(
            group_name="Group two",
            group_url="https://www.facebook.com/groups/2/",
            post_id="202",
            post_url="https://www.facebook.com/groups/2/posts/202",
            text=first.text,
            published_label=None,
            scraped_at="2026-09-17T11:00:00+00:00",
            source_city="lausanne",
        )
        with tempfile.TemporaryDirectory() as directory:
            with PostStore(Path(directory) / "posts.db") as store:
                first_result = store.upsert([first])
                second_result = store.upsert([cross_post])
                self.assertEqual(first_result[:2], (1, 0))
                self.assertEqual(second_result[:2], (0, 1))
                self.assertEqual(store.count(), 1)
                self.assertEqual(
                    second_result[2][dedupe_key(cross_post)],
                    dedupe_key(first),
                )

    def test_does_not_merge_identical_text_across_cities(self) -> None:
        lausanne = RawPost(
            group_name="Lausanne housing",
            group_url="https://www.facebook.com/groups/1/",
            post_id="101",
            post_url="https://www.facebook.com/groups/1/posts/101",
            text="Room available immediately.",
            published_label=None,
            scraped_at="2026-09-24T10:00:00+00:00",
            source_city="lausanne",
        )
        amsterdam = RawPost(
            group_name="Amsterdam housing",
            group_url="https://www.facebook.com/groups/2/",
            post_id="202",
            post_url="https://www.facebook.com/groups/2/posts/202",
            text=lausanne.text,
            published_label=None,
            scraped_at="2026-09-24T10:01:00+00:00",
            source_city="amsterdam",
        )
        with tempfile.TemporaryDirectory() as directory:
            with PostStore(Path(directory) / "posts.db") as store:
                self.assertEqual(store.upsert([lausanne])[:2], (1, 0))
                self.assertEqual(store.upsert([amsterdam])[:2], (1, 0))
                self.assertEqual(store.count(), 2)


if __name__ == "__main__":
    unittest.main()
