from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from fb_automator.models import GroupSource, RawPost
from fb_automator.storage import PostStore

POST_BODY_SELECTOR = '[data-ad-preview="message"], div[dir="auto"]'
POST_PATH = re.compile(r"/groups/[^/]+/(?:posts|permalink)/([^/?#]+)")
SEE_MORE_LABELS = ("See more", "Meer weergeven")
BLOCK_MESSAGE = re.compile(
    r"temporarily blocked|going too fast|try again later|"
    r"tijdelijk geblokkeerd|te snel|probeer het later opnieuw",
    re.IGNORECASE,
)


def canonical_post_url(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.hostname not in {"facebook.com", "www.facebook.com", "m.facebook.com"}:
        return None
    if not POST_PATH.search(parsed.path):
        return None
    return urlunsplit(("https", "www.facebook.com", parsed.path.rstrip("/"), "", ""))


def post_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = POST_PATH.search(urlsplit(url).path)
    return match.group(1) if match else None


class FacebookCollector:
    def __init__(
        self,
        profile_dir: Path,
        database: Path,
        *,
        headless: bool = False,
        scroll_pause: float = 3.0,
    ):
        self.profile_dir = profile_dir
        self.database = database
        self.headless = headless
        self.scroll_pause = scroll_pause

    def login(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(self.profile_dir), headless=False, viewport={"width": 1440, "height": 1000}
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
            print("Sign in to Facebook in the opened browser window.")
            input("When your Facebook home feed is visible, press Enter here to save the session... ")
            if self._looks_logged_out(page):
                context.close()
                raise RuntimeError("Facebook still appears to be logged out; login was not saved.")
            context.close()
        print(f"Facebook session saved under {self.profile_dir}")

    def collect(self, groups: list[GroupSource], max_posts: int, max_scrolls: int) -> None:
        if not self.profile_dir.exists():
            raise RuntimeError("No browser session found. Run `fb-housing login` first.")

        with sync_playwright() as playwright, PostStore(self.database) as store:
            context = playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                viewport={"width": 1440, "height": 1000},
            )
            page = context.pages[0] if context.pages else context.new_page()
            for group in groups:
                print(f"Collecting {group.name}: {group.url}")
                page.goto(group.url, wait_until="domcontentloaded", timeout=60_000)
                self._guard_access(page)
                self._wait_for_feed(page)
                posts = self._collect_group(page, group, max_posts, max_scrolls)
                inserted, updated = store.upsert(posts)
                print(
                    f"  found={len(posts)} new={inserted} refreshed={updated} "
                    f"database_total={store.count()}"
                )
            context.close()

    @staticmethod
    def _looks_logged_out(page: Page) -> bool:
        return page.locator('input[name="email"], input[name="pass"]').count() > 0

    @classmethod
    def _guard_access(cls, page: Page) -> None:
        if cls._looks_logged_out(page) or "/login" in page.url:
            raise RuntimeError("Facebook session expired. Run `fb-housing login` again.")
        if "/checkpoint" in page.url or page.locator(
            'form[action*="checkpoint"], input[name="approvals_code"]'
        ).count():
            raise RuntimeError(
                "Facebook requested an account checkpoint. Collection stopped immediately."
            )
        if page.get_by_text(BLOCK_MESSAGE).count():
            raise RuntimeError(
                "Facebook displayed a temporary-block or rate-limit warning. "
                "Collection stopped immediately."
            )

    @staticmethod
    def _wait_for_feed(page: Page) -> None:
        try:
            page.locator('a[href*="/posts/"]').first.wait_for(
                state="attached", timeout=20_000
            )
            page.wait_for_timeout(2_000)
        except PlaywrightTimeoutError:
            print("  warning: no post links detected; trying extraction anyway")

    def _collect_group(
        self, page: Page, group: GroupSource, max_posts: int, max_scrolls: int
    ) -> list[RawPost]:
        collected: dict[str, RawPost] = {}
        unchanged_rounds = 0

        for _ in range(max_scrolls + 1):
            self._guard_access(page)
            self._expand_visible_posts(page)
            for post in self._extract_visible_posts(page, group):
                key = post.post_id or post.post_url or post.text
                previous_post = collected.get(key)
                if key and (
                    previous_post is None or len(post.text) > len(previous_post.text)
                ):
                    collected[key] = post
                if len(collected) >= max_posts:
                    return list(collected.values())[:max_posts]

            previous = len(collected)
            before_scroll = page.evaluate("document.scrollingElement.scrollTop")
            page.keyboard.press("PageDown")
            wait_time = self.scroll_pause + min(unchanged_rounds * 1.5, 6.0)
            time.sleep(wait_time)
            after_scroll = page.evaluate("document.scrollingElement.scrollTop")
            if after_scroll == before_scroll:
                page.evaluate(
                    "document.scrollingElement.scrollTop += "
                    "Math.round(window.innerHeight * 0.8)"
                )
                time.sleep(wait_time)
            self._guard_access(page)
            self._expand_visible_posts(page)
            for post in self._extract_visible_posts(page, group):
                key = post.post_id or post.post_url or post.text
                previous_post = collected.get(key)
                if key and (
                    previous_post is None or len(post.text) > len(previous_post.text)
                ):
                    collected[key] = post

            if len(collected) == previous:
                unchanged_rounds += 1
                if unchanged_rounds >= 5:
                    break
            else:
                unchanged_rounds = 0

        return list(collected.values())[:max_posts]

    @staticmethod
    def _expand_visible_posts(page: Page) -> None:
        for label in SEE_MORE_LABELS:
            candidates = page.get_by_text(label, exact=True)
            for index in range(min(candidates.count(), 20)):
                try:
                    candidates.nth(index).click(timeout=750)
                except Exception:
                    continue

    @staticmethod
    def _extract_visible_posts(page: Page, group: GroupSource) -> list[RawPost]:
        scraped_at = datetime.now(UTC).isoformat()
        raw_items: list[dict[str, Any]] = page.locator(POST_BODY_SELECTOR).evaluate_all(
            """
            (nodes) => {
              const unique = [...new Set(nodes)];
              return unique.map((message) => {
                const explicitMessage = message.hasAttribute('data-ad-preview') ||
                                        message.hasAttribute('data-ad-comet-preview');
                const text = (message.innerText || message.textContent || '').trim();
                if (!text || (!explicitMessage && text.length < 40)) return null;

                let container = message;
                let hops = 0;
                while (container && container !== document.body) {
                  if (container.querySelector('a[href*="/posts/"]')) break;
                  container = container.parentElement;
                  hops += 1;
                }
                if (!container || container === document.body) return null;
                if (!explicitMessage && hops > 8) return null;

                const links = [...(container || message).querySelectorAll('a[href]')]
                  .map((anchor) => ({
                  href: anchor.href,
                  label: anchor.getAttribute('aria-label') ||
                         anchor.getAttribute('title') ||
                         anchor.textContent || ''
                }));
                return {
                  text,
                  links
                };
              }).filter(Boolean);
            }
            """
        )

        posts: list[RawPost] = []
        for item in raw_items:
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            post_url = None
            published_label = None
            for link in item.get("links", []):
                candidate = canonical_post_url(str(link.get("href", "")))
                if candidate:
                    post_url = candidate
                    published_label = str(link.get("label", "")).strip() or None
                    break
            posts.append(
                RawPost(
                    group_name=group.name,
                    group_url=group.url,
                    post_id=post_id_from_url(post_url),
                    post_url=post_url,
                    text=text,
                    published_label=published_label,
                    scraped_at=scraped_at,
                )
            )
        return posts
