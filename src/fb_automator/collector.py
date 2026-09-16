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

MESSAGE_SELECTOR = '[data-ad-preview="message"]'
POST_PATH = re.compile(r"/groups/[^/]+/(?:posts|permalink)/([^/?#]+)")
SEE_MORE_LABELS = ("See more", "Meer weergeven")


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
                if self._looks_logged_out(page):
                    context.close()
                    raise RuntimeError("Facebook session expired. Run `fb-housing login` again.")
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

    @staticmethod
    def _wait_for_feed(page: Page) -> None:
        try:
            page.locator(MESSAGE_SELECTOR).first.wait_for(state="visible", timeout=20_000)
        except PlaywrightTimeoutError:
            print("  warning: no post messages detected; trying extraction anyway")

    def _collect_group(
        self, page: Page, group: GroupSource, max_posts: int, max_scrolls: int
    ) -> list[RawPost]:
        collected: dict[str, RawPost] = {}
        unchanged_rounds = 0

        for _ in range(max_scrolls + 1):
            self._expand_visible_posts(page)
            for post in self._extract_visible_posts(page, group):
                key = post.post_id or post.post_url or post.text
                if key:
                    collected[key] = post
                if len(collected) >= max_posts:
                    return list(collected.values())[:max_posts]

            previous = len(collected)
            messages = page.locator(MESSAGE_SELECTOR)
            if messages.count():
                try:
                    messages.last.scroll_into_view_if_needed(timeout=2_000)
                except Exception:
                    pass
            page.mouse.wheel(0, 1_800)
            time.sleep(self.scroll_pause)
            self._expand_visible_posts(page)
            for post in self._extract_visible_posts(page, group):
                key = post.post_id or post.post_url or post.text
                if key:
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
        raw_items: list[dict[str, Any]] = page.locator(MESSAGE_SELECTOR).evaluate_all(
            """
            (nodes) => {
              const unique = [...new Set(nodes)];
              return unique.map((message) => {
                let container = message;
                while (container && container !== document.body) {
                  if (container.querySelector('a[href*="/posts/"]')) break;
                  container = container.parentElement;
                }
                const links = [...(container || message).querySelectorAll('a[href]')]
                  .map((anchor) => ({
                  href: anchor.href,
                  label: anchor.getAttribute('aria-label') ||
                         anchor.getAttribute('title') ||
                         anchor.textContent || ''
                  }));
                return {
                  text: (message.innerText || message.textContent || '').trim(),
                  links
                };
              });
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
