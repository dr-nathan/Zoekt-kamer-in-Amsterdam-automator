import unittest

from fb_automator.collector import (
    canonical_post_url,
    merge_post_observations,
    parse_engagement_counts,
    post_id_from_url,
)
from fb_automator.models import RawPost


class PostUrlTests(unittest.TestCase):
    def test_normalizes_group_post_url(self) -> None:
        raw = "https://m.facebook.com/groups/123/posts/456/?tracking=value"
        normalized = canonical_post_url(raw)
        self.assertEqual(normalized, "https://www.facebook.com/groups/123/posts/456")
        self.assertEqual(post_id_from_url(normalized), "456")

    def test_rejects_non_post_url(self) -> None:
        self.assertIsNone(canonical_post_url("https://www.facebook.com/groups/123/"))

    def test_parses_english_and_dutch_engagement_counts(self) -> None:
        candidates = [
            {"aria_label": "See who reacted to this", "text": "1.2K"},
            {"text": "37 comments"},
            {"text": "5 opmerkingen"},
        ]
        self.assertEqual(parse_engagement_counts(candidates), (1200, 37))

    def test_parses_count_in_accessibility_label(self) -> None:
        candidates = [
            {"aria_label": "18 reacties", "text": ""},
            {"aria_label": "3 opmerkingen", "text": ""},
        ]
        self.assertEqual(parse_engagement_counts(candidates), (18, 3))

    def test_does_not_treat_post_age_as_millions_of_comments(self) -> None:
        candidates = [
            {"aria_label": "Like Comment", "text": "40m Comment"},
        ]
        self.assertEqual(parse_engagement_counts(candidates), (None, None))

    def test_merges_richer_text_and_latest_engagement(self) -> None:
        previous = RawPost(
            "Housing", "group", "1", "post", "Longer original text", None,
            "2026-09-16T10:00:00+00:00", 4, 2,
        )
        current = RawPost(
            "Housing", "group", "1", "post", "Short", None,
            "2026-09-16T10:01:00+00:00", 7, 3, ("https://example.com/room.jpg",),
        )
        merged = merge_post_observations(previous, current)
        self.assertEqual(merged.text, "Longer original text")
        self.assertEqual((merged.reaction_count, merged.comment_count), (7, 3))
        self.assertEqual(merged.scraped_at, "2026-09-16T10:01:00+00:00")
        self.assertEqual(merged.image_urls, ("https://example.com/room.jpg",))


if __name__ == "__main__":
    unittest.main()
