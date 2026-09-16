import unittest

from fb_automator.collector import canonical_post_url, post_id_from_url


class PostUrlTests(unittest.TestCase):
    def test_normalizes_group_post_url(self) -> None:
        raw = "https://m.facebook.com/groups/123/posts/456/?tracking=value"
        normalized = canonical_post_url(raw)
        self.assertEqual(normalized, "https://www.facebook.com/groups/123/posts/456")
        self.assertEqual(post_id_from_url(normalized), "456")

    def test_rejects_non_post_url(self) -> None:
        self.assertIsNone(canonical_post_url("https://www.facebook.com/groups/123/"))


if __name__ == "__main__":
    unittest.main()
