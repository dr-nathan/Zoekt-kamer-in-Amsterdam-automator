from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

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
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_posts_group
    ON raw_posts (group_url, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_posts_post_id
    ON raw_posts (post_id);
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
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(SCHEMA)

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
                        text, published_label, first_seen_at, last_seen_at, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(dedupe_key) DO UPDATE SET
                        group_name = excluded.group_name,
                        post_url = COALESCE(excluded.post_url, raw_posts.post_url),
                        text = excluded.text,
                        published_label = COALESCE(
                            excluded.published_label, raw_posts.published_label
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
                        post.scraped_at,
                        post.scraped_at,
                        json.dumps(post.as_dict(), ensure_ascii=False),
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
