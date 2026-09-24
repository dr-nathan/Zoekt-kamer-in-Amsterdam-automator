from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from fb_automator.models import GroupSource, RawPost
from fb_automator.storage import PostStore, dedupe_key

POST_BODY_SELECTOR = (
    '[data-ad-preview="message"], '
    '[data-ad-comet-preview="message"], '
    "blockquote"
)
POST_PATH = re.compile(r"/groups/[^/]+/(?:posts|permalink)/([^/?#]+)")
SEE_MORE_LABELS = ("See more", "Meer weergeven")
BLOCK_MESSAGE = re.compile(
    r"temporarily blocked|going too fast|try again later|"
    r"tijdelijk geblokkeerd|te snel|probeer het later opnieuw",
    re.IGNORECASE,
)
COUNT_TOKEN = r"\d+(?:[.,]\d+)?[Kk]?(?![\dA-Za-z])"
COMMENT_WORDS = r"comments?|commentaren?|opmerkingen?"
REACTION_WORDS = r"reactions?|reacties?|likes?|vind-ik-leuks?"
COMMENT_HINT = re.compile(r"comment|commentaar|opmerking", re.IGNORECASE)
REACTION_HINT = re.compile(
    r"reaction|reactie|reacted|gereageerd|likes?|vind-ik-leuk", re.IGNORECASE
)
IMAGE_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
MAX_IMAGES_PER_POST = 3
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _human_count(value: str) -> int | None:
    compact = re.sub(r"\s+", "", value.strip())
    match = re.fullmatch(r"([\d.,]+)([Kk]?)", compact)
    if not match:
        return None
    number, suffix = match.groups()
    if suffix:
        normalized = number.replace(",", ".")
        if normalized.count(".") > 1:
            parts = normalized.split(".")
            normalized = "".join(parts[:-1]) + "." + parts[-1]
        amount = float(normalized)
        return round(amount * 1_000)
    return int(re.sub(r"[.,]", "", number))


def _engagement_count(
    candidates: list[dict[str, str]], word_pattern: str, hint_pattern: re.Pattern[str]
) -> int | None:
    counts: list[int] = []
    expressions = (
        re.compile(rf"(?P<count>{COUNT_TOKEN})\s*(?:{word_pattern})", re.IGNORECASE),
        re.compile(rf"(?:{word_pattern})\s*[:·-]?\s*(?P<count>{COUNT_TOKEN})", re.IGNORECASE),
    )
    for candidate in candidates:
        label = " ".join(
            candidate.get(field, "") for field in ("aria_label", "title", "text")
        ).strip()
        for expression in expressions:
            for match in expression.finditer(label):
                count = _human_count(match.group("count"))
                if count is not None:
                    counts.append(count)
        hint = " ".join(
            candidate.get(field, "")
            for field in ("aria_label", "title", "href")
        )
        text = candidate.get("text", "").strip()
        if hint_pattern.search(hint) and re.fullmatch(COUNT_TOKEN, text):
            count = _human_count(text)
            if count is not None:
                counts.append(count)
    return max(counts) if counts else None


def parse_engagement_counts(
    candidates: list[dict[str, str]],
) -> tuple[int | None, int | None]:
    return (
        _engagement_count(candidates, REACTION_WORDS, REACTION_HINT),
        _engagement_count(candidates, COMMENT_WORDS, COMMENT_HINT),
    )


def merge_post_observations(previous: RawPost | None, current: RawPost) -> RawPost:
    if previous is None:
        return current
    content = current if len(current.text) > len(previous.text) else previous
    embedded = (
        current.embedded_listing_text
        if len(current.embedded_listing_text) > len(previous.embedded_listing_text)
        else previous.embedded_listing_text
    )
    return replace(
        content,
        scraped_at=current.scraped_at,
        reaction_count=(
            current.reaction_count
            if current.reaction_count is not None
            else previous.reaction_count
        ),
        comment_count=(
            current.comment_count
            if current.comment_count is not None
            else previous.comment_count
        ),
        image_urls=current.image_urls or previous.image_urls,
        source_city=current.source_city or previous.source_city,
        embedded_listing_text=embedded,
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

    @property
    def session_file(self) -> Path:
        return self.profile_dir / "storage-state.json"

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
            context.storage_state(path=str(self.session_file))
            self.session_file.chmod(0o600)
            context.close()
        print(f"Facebook session saved under {self.profile_dir}")

    def export_session(self) -> None:
        if not self.profile_dir.exists():
            raise RuntimeError("No browser session found. Run `fb-housing login` first.")
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(self.profile_dir), headless=True, viewport={"width": 1440, "height": 1000}
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
            self._guard_access(page)
            context.storage_state(path=str(self.session_file))
            self.session_file.chmod(0o600)
            context.close()
        print(f"Transferable Facebook session saved to {self.session_file}")

    def collect(self, groups: list[GroupSource], max_posts: int, max_scrolls: int) -> None:
        if not self.profile_dir.exists():
            raise RuntimeError("No browser session found. Run `fb-housing login` first.")

        with sync_playwright() as playwright, PostStore(self.database) as store:
            context = playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                viewport={"width": 1440, "height": 1000},
            )
            self._restore_session(context)
            page = context.pages[0] if context.pages else context.new_page()
            for group in groups:
                print(f"Collecting {group.name}: {group.url}")
                page.goto(group.url, wait_until="domcontentloaded", timeout=60_000)
                self._guard_access(page)
                self._wait_for_feed(page)
                posts = self._collect_group(page, group, max_posts, max_scrolls)
                inserted, updated, resolved_keys = store.upsert(posts)
                image_count = self._download_images(
                    context, posts, store, resolved_keys
                )
                print(
                    f"  found={len(posts)} new={inserted} refreshed={updated} "
                    f"images={image_count} database_total={store.count()}"
                )
            context.storage_state(path=str(self.session_file))
            self.session_file.chmod(0o600)
            context.close()

    def _restore_session(self, context: BrowserContext) -> None:
        if not self.session_file.exists():
            return
        try:
            payload = json.loads(self.session_file.read_text(encoding="utf-8"))
            cookies = payload.get("cookies", [])
            if cookies:
                context.add_cookies(cookies)
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Could not load transferable Facebook session: {self.session_file}"
            ) from exc

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
            page.locator(POST_BODY_SELECTOR).first.wait_for(
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
                if key:
                    collected[key] = merge_post_observations(collected.get(key), post)
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
                if key:
                    collected[key] = merge_post_observations(collected.get(key), post)

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
            r"""
            (nodes) => {
              const unique = [...new Set(nodes)];
              return unique.map((message) => {
                // Facebook renders the original post inside a blockquote. Comments use
                // very similar div[dir="auto"] nodes, but live inside role=article.
                // Starting from every dir=auto node therefore paired comment text with
                // the parent post's permalink and gallery. Anchor extraction on the
                // post body instead and explicitly reject comment/reply articles.
                const body = message.closest('blockquote') || message;
                if (body.closest('[role="article"]')) return null;
                const text = (body.innerText || body.textContent || '').trim();
                if (!text || text.length < 40) return null;

                // Walk only inside the comment-free post shell and stop at the first
                // ancestor that owns a real gallery image. Expanding "See more" can
                // insert an extra wrapper between the blockquote and that gallery.
                let content = body;
                for (let depth = 0; depth < 6; depth += 1) {
                  const parent = content.parentElement;
                  if (!parent || parent === document.body) break;
                  if (parent.querySelector('[role="article"]') &&
                      !content.querySelector('[role="article"]')) break;
                  content = parent;
                  const hasGalleryImage = [...content.querySelectorAll('img[src]')]
                    .some((image) => {
                      const rect = image.getBoundingClientRect();
                      const width = Math.max(rect.width, image.naturalWidth || 0);
                      const height = Math.max(rect.height, image.naturalHeight || 0);
                      const src = image.currentSrc || image.src || '';
                      return width >= 280 && height >= 160 &&
                        /(?:fbcdn\.net|facebook\.com)/i.test(src);
                    });
                  if (hasGalleryImage) break;
                }

                // Permalinks sit just outside the content wrapper. A comment timestamp
                // may be the only visible /posts/ link in a single-post view; it still
                // identifies the parent post, but prefer the clean permalink when one
                // is available.
                let linkContainer = content;
                let hops = 0;
                let postLinks = [];
                while (linkContainer && linkContainer !== document.body && hops <= 12) {
                  postLinks = [...linkContainer.querySelectorAll('a[href]')].filter(
                    (anchor) => /\/groups\/[^/]+\/(?:posts|permalink)\//
                      .test(anchor.href || '')
                  );
                  if (postLinks.length) break;
                  linkContainer = linkContainer.parentElement;
                  hops += 1;
                }
                if (!linkContainer || linkContainer === document.body || !postLinks.length) {
                  return null;
                }
                postLinks.sort((left, right) => {
                  const leftComment = /[?&]comment_id=/.test(left.href || '');
                  const rightComment = /[?&]comment_id=/.test(right.href || '');
                  return Number(leftComment) - Number(rightComment);
                });

                const links = postLinks
                  .map((anchor) => ({
                  href: anchor.href,
                  label: anchor.getAttribute('aria-label') ||
                         anchor.getAttribute('title') ||
                         anchor.textContent || ''
                }));
                const engagement = [...content.querySelectorAll(
                  '[aria-label], [title], [role="button"], a[href]'
                )].map((element) => ({
                  aria_label: element.getAttribute('aria-label') || '',
                  title: element.getAttribute('title') || '',
                  text: (element.innerText || element.textContent || '').trim().slice(0, 100),
                  href: element.getAttribute('href') || ''
                })).filter((item) => {
                  const value = `${item.aria_label} ${item.title} ${item.text} ${item.href}`
                    .toLowerCase();
                  return /comment|commentaar|opmerking|react|gereageerd|like|vind-ik-leuk/
                    .test(value);
                }).slice(0, 100);
                const images = [...content.querySelectorAll('img[src]')]
                  .map((image) => {
                    const rect = image.getBoundingClientRect();
                    const naturalWidth = image.naturalWidth || 0;
                    const naturalHeight = image.naturalHeight || 0;
                    const profileLink = image.closest(
                      'a[href*="/profile.php"], a[href*="/people/"], a[href*="/user/"]'
                    );
                    return {
                      src: image.currentSrc || image.src || '',
                      width: Math.max(rect.width, naturalWidth),
                      height: Math.max(rect.height, naturalHeight),
                      profile: Boolean(profileLink)
                    };
                  })
                  .filter((image) => {
                    if (!image.src.startsWith('https://') || image.profile) return false;
                    if (image.width < 280 || image.height < 160) return false;
                    return /(?:fbcdn\.net|facebook\.com)/i.test(image.src);
                  })
                  .sort((a, b) => (b.width * b.height) - (a.width * a.height))
                  .map((image) => image.src)
                  .filter((src, index, all) => all.indexOf(src) === index)
                  .slice(0, 3);
                // Marketplace-style listing cards are separate from the authored
                // post body. Their outer link contains an action button and carries
                // structured text such as price, municipality, and listing title.
                const embeddedListingText = [...content.querySelectorAll('a, [role="link"]')]
                  .filter((link) => link.querySelector('button, [role="button"]'))
                  .map((link) => (link.innerText || link.textContent || '').trim())
                  .filter((value) => value.length >= 8 && value.length <= 800)
                  .filter((value, index, all) => all.indexOf(value) === index)
                  .slice(0, 3)
                  .join('\n---\n');
                return {
                  text,
                  embedded_listing_text: embeddedListingText,
                  links,
                  engagement,
                  images
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
            reaction_count, comment_count = parse_engagement_counts(
                item.get("engagement", [])
            )
            posts.append(
                RawPost(
                    group_name=group.name,
                    group_url=group.url,
                    post_id=post_id_from_url(post_url),
                    post_url=post_url,
                    text=text,
                    published_label=published_label,
                    scraped_at=scraped_at,
                    reaction_count=reaction_count,
                    comment_count=comment_count,
                    image_urls=tuple(
                        str(url) for url in item.get("images", []) if str(url)
                    ),
                    source_city=group.city,
                    embedded_listing_text=str(
                        item.get("embedded_listing_text", "")
                    ).strip(),
                )
            )
        return posts

    def _download_images(
        self,
        context: BrowserContext,
        posts: list[RawPost],
        store: PostStore,
        resolved_keys: dict[str, str],
    ) -> int:
        downloaded = 0
        for post in posts:
            proposed_key = dedupe_key(post)
            raw_post_key = resolved_keys.get(proposed_key, proposed_key)
            for position, source_url in enumerate(
                post.image_urls[:MAX_IMAGES_PER_POST]
            ):
                stored = store.stored_image_path(raw_post_key, source_url)
                if stored and (self.database.parent / stored).is_file():
                    continue
                try:
                    response = context.request.get(
                        source_url,
                        headers={"Referer": post.post_url or post.group_url},
                        timeout=30_000,
                    )
                    content_type = response.headers.get("content-type", "").split(";", 1)[0]
                    extension = IMAGE_TYPES.get(content_type)
                    if not response.ok or extension is None:
                        continue
                    body = response.body()
                    if not body or len(body) > MAX_IMAGE_BYTES:
                        continue
                except Exception:
                    continue

                digest = hashlib.sha256(body).hexdigest()[:16]
                relative_path = (
                    Path("images")
                    / raw_post_key
                    / f"{position}-{digest}.{extension}"
                )
                target = self.database.parent / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)
                store.upsert_image(
                    raw_post_key=raw_post_key,
                    position=position,
                    source_url=source_url,
                    local_path=relative_path.as_posix(),
                    content_type=content_type,
                    observed_at=post.scraped_at,
                )
                downloaded += 1
        return downloaded
