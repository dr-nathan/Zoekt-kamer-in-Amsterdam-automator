import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from playwright.sync_api import sync_playwright

from fb_automator.collector import (
    FacebookCollector,
    POST_BODY_SELECTOR,
    canonical_post_url,
    merge_post_observations,
    parse_engagement_counts,
    post_id_from_url,
)
from fb_automator.models import GroupSource, RawPost
from fb_automator.storage import PostStore, dedupe_key


class PostUrlTests(unittest.TestCase):
    def test_post_selector_excludes_generic_comment_text_nodes(self) -> None:
        self.assertIn("blockquote", POST_BODY_SELECTOR)
        self.assertNotIn('div[dir="auto"]', POST_BODY_SELECTOR)

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
            embedded_listing_text="CHF1,300 · Lausanne",
        )
        merged = merge_post_observations(previous, current)
        self.assertEqual(merged.text, "Longer original text")
        self.assertEqual((merged.reaction_count, merged.comment_count), (7, 3))
        self.assertEqual(merged.scraped_at, "2026-09-16T10:01:00+00:00")
        self.assertEqual(merged.image_urls, ("https://example.com/room.jpg",))
        self.assertEqual(merged.embedded_listing_text, "CHF1,300 · Lausanne")

    def test_captures_embedded_listing_card_separately_from_post_text(self) -> None:
        html = """
        <section>
          <a href="https://www.facebook.com/groups/123/posts/456">2 h</a>
          <div class="media-shell">
            <blockquote>
              Studio lumineux de 30 m² à Lausanne, disponible dès novembre.
            </blockquote>
            <img src="https://scontent.example.fbcdn.net/room.jpg"
                 width="320" height="200" alt="Studio">
          </div>
          <a href="#" role="link">
            <span>CHF1,300 · Lausanne, VD · Studio 30 m²</span>
            <button type="button">Message</button>
          </a>
        </section>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content(html)
            posts = FacebookCollector._extract_visible_posts(
                page,
                GroupSource(
                    name="Housing",
                    url="https://www.facebook.com/groups/123",
                    city="lausanne",
                ),
            )
            browser.close()

        self.assertEqual(len(posts), 1)
        self.assertNotIn("CHF1,300", posts[0].text)
        self.assertIn("CHF1,300", posts[0].embedded_listing_text)

    def test_python_extraction_rejects_image_urls_outside_host_boundaries(self) -> None:
        page = MagicMock()
        allowed = "https://scontent.example.fbcdn.net/room.jpg?signature=unchanged"
        page.locator.return_value.evaluate_all.return_value = [{
            "text": "Synthetic housing offer long enough to extract safely.",
            "images": [
                "https://127.0.0.1/image?facebook.com",
                "https://facebook.com.attacker.invalid/room.jpg",
                "https://attacker.invalid/fbcdn.net/room.jpg",
                allowed,
            ],
        }]
        posts = FacebookCollector._extract_visible_posts(
            page, GroupSource("Synthetic", "https://www.facebook.com/groups/synthetic", "lausanne")
        )
        self.assertEqual(posts[0].image_urls, (allowed,))

    def test_browser_extraction_filters_untrusted_image_hosts(self) -> None:
        # Abort all image requests: this checks DOM selection without contacting
        # Facebook, the CDN, or the intentionally invalid test destinations.
        html = """
        <section>
          <a href="https://www.facebook.com/groups/123/posts/456">2 h</a>
          <div><blockquote>
            Studio lumineux de 30 m² à Lausanne, disponible dès novembre.
          </blockquote>
          <img src="https://facebook.com.attacker.invalid/room.jpg" width="600" height="400">
          <img src="https://127.0.0.1/image?facebook.com" width="600" height="400">
          <img src="https://scontent.example.fbcdn.net/room.jpg" width="320" height="200">
          </div>
        </section>
        """
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content(html)
            posts = FacebookCollector._extract_visible_posts(
                page, GroupSource("Synthetic", "https://www.facebook.com/groups/123", "lausanne")
            )
            browser.close()
        self.assertEqual(posts[0].image_urls, ("https://scontent.example.fbcdn.net/room.jpg",))


class ImageStorageTests(unittest.TestCase):
    def test_download_uses_cookie_free_fetch_and_caches_existing_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "listings.db"
            collector = FacebookCollector(Path(directory) / "profile", database)
            context = MagicMock()
            url = "https://scontent.example.fbcdn.net/room.jpg?signature=unchanged"
            post = RawPost(
                "Synthetic", "https://www.facebook.com/groups/synthetic", "42",
                "https://www.facebook.com/groups/synthetic/posts/42",
                "Synthetic housing offer", "Today", "2026-11-18T12:00:00+00:00",
                image_urls=(url,), source_city="lausanne",
            )
            with PostStore(database) as store, patch(
                "fb_automator.collector.download_image", return_value=(b"synthetic-image", "image/jpeg")
            ) as fetch:
                _, _, resolved = store.upsert([post])
                self.assertEqual(collector._download_images(context, [post], store, resolved), 1)
                path = store.stored_image_path(dedupe_key(post), url)
                self.assertEqual((database.parent / path).read_bytes(), b"synthetic-image")
                self.assertEqual(collector._download_images(context, [post], store, resolved), 0)
            fetch.assert_called_once_with(url, referer=post.post_url)
            context.request.get.assert_not_called()

    def test_rejected_download_does_not_create_a_media_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "listings.db"
            collector = FacebookCollector(Path(directory) / "profile", database)
            post = RawPost("Synthetic", "group", None, None, "Synthetic", "Today", "2026-11-18",
                           image_urls=("https://127.0.0.1/image?facebook.com",))
            with PostStore(database) as store, patch(
                "fb_automator.collector.download_image", return_value=None
            ):
                _, _, resolved = store.upsert([post])
                self.assertEqual(collector._download_images(MagicMock(), [post], store, resolved), 0)
            self.assertFalse((database.parent / "images").exists())


class SessionTransferTests(unittest.TestCase):
    def test_restore_session_loads_cookies_and_local_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile_dir = Path(directory)
            (profile_dir / "storage-state.json").write_text(
                json.dumps(
                    {
                        "cookies": [{"name": "c_user", "value": "123"}],
                        "origins": [
                            {
                                "origin": "https://www.facebook.com",
                                "localStorage": [{"name": "locale", "value": "fr_FR"}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            collector = FacebookCollector(profile_dir, profile_dir / "listings.db")
            context = MagicMock()

            collector._restore_session(context)

            context.add_cookies.assert_called_once_with(
                [{"name": "c_user", "value": "123"}]
            )
            init_script = context.add_init_script.call_args.kwargs["script"]
            self.assertIn("https://www.facebook.com", init_script)
            self.assertIn("localStorage.setItem", init_script)

    def test_export_session_restores_transferable_state_before_navigation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile_dir = Path(directory)
            session_file = profile_dir / "storage-state.json"
            session_file.write_text('{"cookies": []}', encoding="utf-8")
            collector = FacebookCollector(profile_dir, profile_dir / "listings.db")
            context = MagicMock()
            page = MagicMock()
            context.pages = [page]
            playwright = MagicMock()
            playwright.chromium.launch_persistent_context.return_value = context
            manager = MagicMock()
            manager.__enter__.return_value = playwright

            with (
                patch("fb_automator.collector.sync_playwright", return_value=manager),
                patch.object(collector, "_restore_session") as restore_session,
                patch.object(collector, "_guard_access"),
            ):
                collector.export_session()

            restore_session.assert_called_once_with(context)
            page.goto.assert_called_once_with(
                "https://www.facebook.com/", wait_until="domcontentloaded"
            )


if __name__ == "__main__":
    unittest.main()
