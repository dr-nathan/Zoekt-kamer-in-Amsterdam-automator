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
    group_name TEXT NOT NULL,
    group_url TEXT NOT NULL,
    post_id TEXT,
    post_url TEXT,
    text TEXT NOT NULL,
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
    FOREIGN KEY (raw_post_key) REFERENCES raw_posts(dedupe_key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_listings_kind_rent
    ON listings (listing_kind, monthly_rent);
CREATE INDEX IF NOT EXISTS idx_listings_location
    ON listings (location_text);
"""


def dedupe_key(post: RawPost) -> str:
    stable_value = post.post_id or post.post_url or post.text
    material = f"{post.group_url}\n{stable_value}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


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

    def _migrate_raw_post_columns(self) -> None:
        existing = {
            row[1] for row in self.connection.execute("PRAGMA table_info(raw_posts)")
        }
        additions = {
            "reaction_count": "INTEGER",
            "comment_count": "INTEGER",
        }
        with self.connection:
            for name, declaration in additions.items():
                if name not in existing:
                    self.connection.execute(
                        f"ALTER TABLE raw_posts ADD COLUMN {name} {declaration}"
                    )

    def _migrate_listing_columns(self) -> None:
        existing = {
            row[1] for row in self.connection.execute("PRAGMA table_info(listings)")
        }
        additions = {
            "city": "TEXT",
            "neighborhood": "TEXT",
            "summary": "TEXT NOT NULL DEFAULT ''",
            "source_hash": "TEXT NOT NULL DEFAULT ''",
            "language_requirement": "TEXT NOT NULL DEFAULT 'unknown'",
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

    def upsert(self, posts: list[RawPost]) -> tuple[int, int]:
        inserted = 0
        updated = 0
        with self.connection:
            for post in posts:
                key = dedupe_key(post)
                exists = self.connection.execute(
                    "SELECT 1 FROM raw_posts WHERE dedupe_key = ?", (key,)
                ).fetchone()
                self.connection.execute(
                    """
                    INSERT INTO raw_posts (
                        dedupe_key, group_name, group_url, post_id, post_url,
                        text, published_label, reaction_count, comment_count,
                        first_seen_at, last_seen_at, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(dedupe_key) DO UPDATE SET
                        group_name = excluded.group_name,
                        post_url = COALESCE(excluded.post_url, raw_posts.post_url),
                        text = excluded.text,
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
                        post.group_name,
                        post.group_url,
                        post.post_id,
                        post.post_url,
                        post.text,
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
        return inserted, updated

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
            SELECT r.dedupe_key, r.text, r.last_seen_at,
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
                "last_seen_at": last_seen_at,
                "source_hash": source_hash,
                "extraction_version": extraction_version,
            }
            for key, text, last_seen_at, source_hash, extraction_version in rows
        ]

    def upsert_listings(self, listings: list[ListingAttributes]) -> None:
        columns = (
            "raw_post_key", "listing_kind", "monthly_rent", "utilities",
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
