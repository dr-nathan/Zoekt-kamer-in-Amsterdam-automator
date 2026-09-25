from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from fb_automator.listing_models import ListingAttributes
from fb_automator.models import RawPost

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_posts (
    dedupe_key TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL DEFAULT '',
    source_city TEXT NOT NULL DEFAULT '',
    group_name TEXT NOT NULL,
    group_url TEXT NOT NULL,
    post_id TEXT,
    post_url TEXT,
    text TEXT NOT NULL,
    embedded_listing_text TEXT NOT NULL DEFAULT '',
    published_label TEXT,
    reaction_count INTEGER,
    comment_count INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_posts_group
    ON raw_posts (group_url, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_posts_post_id
    ON raw_posts (post_id);

CREATE TABLE IF NOT EXISTS engagement_snapshots (
    raw_post_key TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    reaction_count INTEGER,
    comment_count INTEGER,
    PRIMARY KEY (raw_post_key, observed_at),
    FOREIGN KEY (raw_post_key) REFERENCES raw_posts(dedupe_key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_engagement_snapshots_post
    ON engagement_snapshots (raw_post_key, observed_at DESC);

CREATE TABLE IF NOT EXISTS post_images (
    raw_post_key TEXT NOT NULL,
    position INTEGER NOT NULL,
    source_url TEXT NOT NULL,
    local_path TEXT,
    content_type TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (raw_post_key, position),
    FOREIGN KEY (raw_post_key) REFERENCES raw_posts(dedupe_key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_post_images_source
    ON post_images (raw_post_key, source_url);

CREATE TABLE IF NOT EXISTS listings (
    raw_post_key TEXT PRIMARY KEY,
    listing_kind TEXT NOT NULL,
    monthly_rent REAL,
    currency TEXT NOT NULL DEFAULT 'CHF',
    utilities TEXT NOT NULL,
    deposit_amount REAL,
    deposit_months REAL,
    room_size_m2 REAL,
    property_size_m2 REAL,
    location_text TEXT,
    city TEXT,
    neighborhood TEXT,
    available_from TEXT,
    available_to TEXT,
    lease_type TEXT NOT NULL,
    registration TEXT NOT NULL,
    furnishing TEXT NOT NULL,
    gender TEXT NOT NULL,
    age_min INTEGER,
    age_max INTEGER,
    language_requirement TEXT NOT NULL,
    internationals TEXT NOT NULL,
    applicant_status TEXT NOT NULL,
    private_bathroom INTEGER,
    amenities_json TEXT NOT NULL,
    particularities_json TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL,
    source_hash TEXT NOT NULL DEFAULT '',
    extraction_version TEXT NOT NULL,
    extracted_at TEXT NOT NULL,
    primary_image_position INTEGER,
    image_quality_score INTEGER,
    image_review_hash TEXT NOT NULL DEFAULT '',
    image_review_version TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (raw_post_key) REFERENCES raw_posts(dedupe_key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_listings_kind_rent
    ON listings (listing_kind, monthly_rent);
CREATE INDEX IF NOT EXISTS idx_listings_location
    ON listings (location_text);

CREATE TABLE IF NOT EXISTS subscribers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id INTEGER NOT NULL,
    channel_type TEXT NOT NULL CHECK(channel_type IN ('email', 'telegram')),
    destination TEXT,
    destination_fingerprint TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    verification_token_hash TEXT,
    verified_at TEXT,
    unsubscribed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (subscriber_id) REFERENCES subscribers(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_channels_destination
    ON notification_channels (channel_type, destination_fingerprint)
    WHERE destination_fingerprint IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_notification_channels_status
    ON notification_channels (status, channel_type);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_channels_verification
    ON notification_channels (verification_token_hash)
    WHERE verification_token_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS saved_searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id INTEGER NOT NULL,
    city TEXT NOT NULL,
    area TEXT NOT NULL DEFAULT '',
    max_rent INTEGER,
    min_size INTEGER,
    registration TEXT NOT NULL DEFAULT 'any',
    particularity TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (subscriber_id) REFERENCES subscribers(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_saved_searches_unique
    ON saved_searches (
        subscriber_id, city, area, IFNULL(max_rent, -1), IFNULL(min_size, -1),
        registration, particularity
    );
CREATE INDEX IF NOT EXISTS idx_saved_searches_subscriber
    ON saved_searches (subscriber_id, active);

CREATE TABLE IF NOT EXISTS digest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_key TEXT NOT NULL UNIQUE,
    local_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    channel_count INTEGER NOT NULL DEFAULT 0,
    sent_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_digest_runs_date
    ON digest_runs (local_date DESC);

CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    digest_run_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    local_date TEXT NOT NULL,
    status TEXT NOT NULL,
    provider_message_id TEXT,
    error_summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    sent_at TEXT,
    FOREIGN KEY (digest_run_id) REFERENCES digest_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (channel_id) REFERENCES notification_channels(id) ON DELETE CASCADE,
    UNIQUE (channel_id, local_date)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_channel
    ON deliveries (channel_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_deliveries_status
    ON deliveries (status, created_at DESC);

CREATE TABLE IF NOT EXISTS delivery_items (
    delivery_id INTEGER NOT NULL,
    listing_key TEXT NOT NULL,
    PRIMARY KEY (delivery_id, listing_key),
    FOREIGN KEY (delivery_id) REFERENCES deliveries(id) ON DELETE CASCADE,
    FOREIGN KEY (listing_key) REFERENCES listings(raw_post_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    error_summary TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_job_runs_name_started
    ON job_runs (job_name, started_at DESC);

CREATE TABLE IF NOT EXISTS admin_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_events_created
    ON admin_events (created_at DESC);
"""


def dedupe_key(post: RawPost) -> str:
    stable_value = post.post_id or post.post_url or post.text
    material = f"{post.group_url}\n{stable_value}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PostStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(SCHEMA)
        self._migrate_raw_post_columns()
        self._migrate_listing_columns()
        self.connection.execute("PRAGMA optimize")

    def _migrate_raw_post_columns(self) -> None:
        existing = {
            row[1] for row in self.connection.execute("PRAGMA table_info(raw_posts)")
        }
        additions = {
            "reaction_count": "INTEGER",
            "comment_count": "INTEGER",
            "content_hash": "TEXT NOT NULL DEFAULT ''",
            "source_city": "TEXT NOT NULL DEFAULT ''",
            "embedded_listing_text": "TEXT NOT NULL DEFAULT ''",
        }
        with self.connection:
            for name, declaration in additions.items():
                if name not in existing:
                    self.connection.execute(
                        f"ALTER TABLE raw_posts ADD COLUMN {name} {declaration}"
                    )
            rows = self.connection.execute(
                "SELECT dedupe_key, text FROM raw_posts WHERE content_hash = ''"
            ).fetchall()
            self.connection.executemany(
                "UPDATE raw_posts SET content_hash = ? WHERE dedupe_key = ?",
                [(content_hash(text), key) for key, text in rows],
            )
            self.connection.execute(
                """
                UPDATE raw_posts
                SET source_city = CASE
                    WHEN lower(group_name) LIKE '%amsterdam%' THEN 'amsterdam'
                    WHEN lower(group_name) LIKE '%lausanne%' THEN 'lausanne'
                    ELSE source_city
                END
                WHERE source_city = ''
                """
            )
            self.connection.execute(
                "DROP INDEX IF EXISTS idx_raw_posts_content_hash"
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_raw_posts_content_city "
                "ON raw_posts (content_hash, source_city)"
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_raw_posts_city_age "
                "ON raw_posts (source_city, first_seen_at DESC)"
            )

    def _migrate_listing_columns(self) -> None:
        existing = {
            row[1] for row in self.connection.execute("PRAGMA table_info(listings)")
        }
        additions = {
            "currency": "TEXT NOT NULL DEFAULT 'CHF'",
            "city": "TEXT",
            "neighborhood": "TEXT",
            "summary": "TEXT NOT NULL DEFAULT ''",
            "source_hash": "TEXT NOT NULL DEFAULT ''",
            "language_requirement": "TEXT NOT NULL DEFAULT 'unknown'",
            "primary_image_position": "INTEGER",
            "image_quality_score": "INTEGER",
            "image_review_hash": "TEXT NOT NULL DEFAULT ''",
            "image_review_version": "TEXT NOT NULL DEFAULT ''",
        }
        with self.connection:
            for name, declaration in additions.items():
                if name not in existing:
                    self.connection.execute(
                        f"ALTER TABLE listings ADD COLUMN {name} {declaration}"
                    )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "PostStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def upsert(self, posts: list[RawPost]) -> tuple[int, int, dict[str, str]]:
        inserted = 0
        updated = 0
        resolved_keys: dict[str, str] = {}
        with self.connection:
            for post in posts:
                proposed_key = dedupe_key(post)
                post_content_hash = content_hash(post.text)
                content_match = self.connection.execute(
                    """
                    SELECT dedupe_key FROM raw_posts
                    WHERE content_hash = ? AND source_city = ?
                    ORDER BY first_seen_at
                    LIMIT 1
                    """,
                    (post_content_hash, post.source_city),
                ).fetchone()
                key = str(content_match[0]) if content_match else proposed_key
                resolved_keys[proposed_key] = key
                exists = self.connection.execute(
                    "SELECT 1 FROM raw_posts WHERE dedupe_key = ?", (key,)
                ).fetchone()
                self.connection.execute(
                    """
                    INSERT INTO raw_posts (
                        dedupe_key, content_hash, source_city, group_name, group_url, post_id, post_url,
                        text, embedded_listing_text, published_label, reaction_count, comment_count,
                        first_seen_at, last_seen_at, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(dedupe_key) DO UPDATE SET
                        content_hash = excluded.content_hash,
                        source_city = COALESCE(NULLIF(excluded.source_city, ''), raw_posts.source_city),
                        post_url = COALESCE(raw_posts.post_url, excluded.post_url),
                        text = excluded.text,
                        embedded_listing_text = CASE
                            WHEN length(excluded.embedded_listing_text) >=
                                 length(raw_posts.embedded_listing_text)
                            THEN excluded.embedded_listing_text
                            ELSE raw_posts.embedded_listing_text
                        END,
                        published_label = COALESCE(
                            excluded.published_label, raw_posts.published_label
                        ),
                        reaction_count = COALESCE(
                            excluded.reaction_count, raw_posts.reaction_count
                        ),
                        comment_count = COALESCE(
                            excluded.comment_count, raw_posts.comment_count
                        ),
                        last_seen_at = excluded.last_seen_at,
                        raw_json = excluded.raw_json
                    """,
                    (
                        key,
                        post_content_hash,
                        post.source_city,
                        post.group_name,
                        post.group_url,
                        post.post_id,
                        post.post_url,
                        post.text,
                        post.embedded_listing_text,
                        post.published_label,
                        post.reaction_count,
                        post.comment_count,
                        post.scraped_at,
                        post.scraped_at,
                        json.dumps(post.as_dict(), ensure_ascii=False),
                    ),
                )
                if post.reaction_count is not None or post.comment_count is not None:
                    self.connection.execute(
                        """
                        INSERT INTO engagement_snapshots (
                            raw_post_key, observed_at, reaction_count, comment_count
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(raw_post_key, observed_at) DO UPDATE SET
                            reaction_count = excluded.reaction_count,
                            comment_count = excluded.comment_count
                        """,
                        (
                            key,
                            post.scraped_at,
                            post.reaction_count,
                            post.comment_count,
                        ),
                    )
                if exists:
                    updated += 1
                else:
                    inserted += 1
        return inserted, updated, resolved_keys

    def count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) FROM raw_posts").fetchone()
        return int(row[0])

    def stored_image_path(self, raw_post_key: str, source_url: str) -> str | None:
        row = self.connection.execute(
            """
            SELECT local_path
            FROM post_images
            WHERE raw_post_key = ? AND source_url = ? AND local_path IS NOT NULL
            LIMIT 1
            """,
            (raw_post_key, source_url),
        ).fetchone()
        return str(row[0]) if row else None

    def upsert_image(
        self,
        *,
        raw_post_key: str,
        position: int,
        source_url: str,
        local_path: str,
        content_type: str,
        observed_at: str,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO post_images (
                    raw_post_key, position, source_url, local_path, content_type,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(raw_post_key, position) DO UPDATE SET
                    source_url = excluded.source_url,
                    local_path = excluded.local_path,
                    content_type = excluded.content_type,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    raw_post_key,
                    position,
                    source_url,
                    local_path,
                    content_type,
                    observed_at,
                    observed_at,
                ),
            )

    def raw_posts_for_extraction(self) -> list[dict[str, str | None]]:
        rows = self.connection.execute(
            """
            SELECT r.dedupe_key, r.text, r.embedded_listing_text,
                   r.source_city, r.last_seen_at,
                   l.source_hash, l.extraction_version
            FROM raw_posts AS r
            LEFT JOIN listings AS l ON l.raw_post_key = r.dedupe_key
            ORDER BY r.last_seen_at DESC
            """
        )
        return [
            {
                "dedupe_key": key,
                "text": text,
                "embedded_listing_text": embedded_listing_text,
                "source_city": source_city,
                "last_seen_at": last_seen_at,
                "source_hash": source_hash,
                "extraction_version": extraction_version,
            }
            for (
                key,
                text,
                embedded_listing_text,
                source_city,
                last_seen_at,
                source_hash,
                extraction_version,
            ) in rows
        ]

    def listings_for_image_review(self) -> list[dict[str, object]]:
        listings = self.connection.execute(
            """
            SELECT raw_post_key, summary, image_review_hash, image_review_version
            FROM listings
            WHERE listing_kind = 'offer'
            ORDER BY extracted_at DESC
            """
        ).fetchall()
        results: list[dict[str, object]] = []
        for key, summary, review_hash, review_version in listings:
            images = self.connection.execute(
                """
                SELECT position, local_path, content_type
                FROM post_images
                WHERE raw_post_key = ? AND local_path IS NOT NULL
                ORDER BY position
                """,
                (key,),
            ).fetchall()
            if images:
                results.append(
                    {
                        "raw_post_key": str(key),
                        "summary": str(summary),
                        "image_review_hash": str(review_hash or ""),
                        "image_review_version": str(review_version or ""),
                        "images": [
                            {
                                "position": int(position),
                                "local_path": str(local_path),
                                "content_type": str(content_type or "image/jpeg"),
                            }
                            for position, local_path, content_type in images
                        ],
                    }
                )
        return results

    def update_image_review(
        self,
        *,
        raw_post_key: str,
        primary_image_position: int | None,
        image_quality_score: int,
        image_review_hash: str,
        image_review_version: str,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE listings
                SET primary_image_position = ?, image_quality_score = ?,
                    image_review_hash = ?, image_review_version = ?
                WHERE raw_post_key = ?
                """,
                (
                    primary_image_position,
                    image_quality_score,
                    image_review_hash,
                    image_review_version,
                    raw_post_key,
                ),
            )

    def upsert_listings(self, listings: list[ListingAttributes]) -> None:
        columns = (
            "raw_post_key", "listing_kind", "monthly_rent", "currency", "utilities",
            "deposit_amount", "deposit_months", "room_size_m2", "property_size_m2",
            "location_text", "city", "neighborhood", "available_from",
            "available_to", "lease_type",
            "registration", "furnishing", "gender", "age_min", "age_max",
            "language_requirement", "internationals", "applicant_status",
            "private_bathroom", "amenities_json", "particularities_json",
            "summary", "evidence_json", "source_hash", "extraction_version",
            "extracted_at",
        )
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(
            f"{column} = excluded.{column}" for column in columns if column != "raw_post_key"
        )
        sql = (
            f"INSERT INTO listings ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(raw_post_key) DO UPDATE SET {updates}"
        )
        rows = [
            (
                item.raw_post_key,
                item.listing_kind.value,
                item.monthly_rent,
                item.currency.value,
                item.utilities.value,
                item.deposit_amount,
                item.deposit_months,
                item.room_size_m2,
                item.property_size_m2,
                item.location_text,
                item.city,
                item.neighborhood.value if item.neighborhood is not None else None,
                item.available_from,
                item.available_to,
                item.lease_type.value,
                item.registration.value,
                item.furnishing.value,
                item.gender.value,
                item.age_min,
                item.age_max,
                item.language_requirement.value,
                item.internationals.value,
                item.applicant_status.value,
                None if item.private_bathroom is None else int(item.private_bathroom),
                json.dumps(item.amenities, ensure_ascii=False),
                json.dumps(item.particularities, ensure_ascii=False),
                item.summary,
                json.dumps(item.evidence_jsonable(), ensure_ascii=False),
                item.source_hash,
                item.extraction_version,
                item.extracted_at,
            )
            for item in listings
        ]
        with self.connection:
            self.connection.executemany(sql, rows)

    def listing_count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) FROM listings").fetchone()
        return int(row[0])
