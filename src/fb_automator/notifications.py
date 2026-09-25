from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable, Iterator

from fb_automator.storage import SCHEMA


AMSTERDAM_TIMEZONE = "Europe/Amsterdam"
ACTIVE_CHANNEL_STATUSES = {"active"}
VALID_CHANNEL_STATUSES = {"pending", "active", "paused", "unsubscribed", "bounced"}


@dataclass(frozen=True, slots=True)
class NotificationSettings:
    base_url: str
    app_secret: str
    resend_api_key: str
    resend_from: str
    resend_webhook_secret: str
    telegram_bot_token: str
    telegram_bot_username: str
    telegram_webhook_secret: str

    @classmethod
    def from_env(cls) -> "NotificationSettings":
        return cls(
            base_url=os.environ.get("PUBLIC_BASE_URL", "https://facebookrooms.nl").rstrip("/"),
            app_secret=os.environ.get("APP_SECRET", ""),
            resend_api_key=os.environ.get("RESEND_API_KEY", ""),
            resend_from=os.environ.get(
                "RESEND_FROM", "Chineur2000 <alerts@facebookrooms.nl>"
            ),
            resend_webhook_secret=os.environ.get("RESEND_WEBHOOK_SECRET", ""),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_bot_username=os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@"),
            telegram_webhook_secret=os.environ.get("TELEGRAM_WEBHOOK_SECRET", ""),
        )

    @property
    def email_enabled(self) -> bool:
        return bool(self.app_secret and self.resend_api_key and self.resend_from)

    @property
    def telegram_enabled(self) -> bool:
        return bool(
            self.app_secret
            and self.telegram_bot_token
            and self.telegram_bot_username
            and self.telegram_webhook_secret
        )


@dataclass(frozen=True, slots=True)
class SearchSpec:
    city: str
    area: str = ""
    max_rent: int | None = None
    min_size: int | None = None
    registration: str = "any"
    particularity: str = ""


@dataclass(frozen=True, slots=True)
class ChannelRecord:
    id: int
    subscriber_id: int
    channel_type: str
    destination: str
    status: str
    verified_at: str | None
    display_name: str
    created_at: str

    @property
    def masked_destination(self) -> str:
        return mask_destination(self.channel_type, self.destination)


def utc_now() -> datetime:
    return datetime.now(UTC)


def validate_email(value: str) -> str:
    normalized = value.strip().casefold()
    _, parsed = parseaddr(normalized)
    if (
        parsed != normalized
        or len(normalized) > 254
        or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized)
    ):
        raise ValueError("Adresse e-mail invalide.")
    return normalized


def mask_destination(channel_type: str, destination: str | None) -> str:
    if not destination:
        return "En attente de connexion"
    if channel_type == "email" and "@" in destination:
        local, domain = destination.split("@", 1)
        visible = local[:2] if len(local) > 2 else local[:1]
        return f"{visible}…@{domain}"
    if channel_type == "telegram":
        if destination.startswith("@"):
            return destination[:3] + "…"
        return "Telegram connecté"
    return "Destination masquée"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def destination_fingerprint(channel_type: str, destination: str) -> str:
    material = f"{channel_type}:{destination.strip().casefold()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def sign_action(secret: str, purpose: str, channel_id: int) -> str:
    if not secret:
        raise RuntimeError("APP_SECRET is not configured.")
    payload = f"v1|{purpose}|{channel_id}".encode("utf-8")
    payload_text = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()[:18]
    signature_text = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{payload_text}.{signature_text}"


def verify_action(secret: str, purpose: str, token: str) -> int | None:
    try:
        payload_text, signature_text = token.split(".", 1)
        payload = base64.urlsafe_b64decode(payload_text + "=" * (-len(payload_text) % 4))
        signature = base64.urlsafe_b64decode(
            signature_text + "=" * (-len(signature_text) % 4)
        )
        version, actual_purpose, channel_text = payload.decode("utf-8").split("|", 2)
        expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()[:18]
        if version != "v1" or actual_purpose != purpose:
            return None
        if not hmac.compare_digest(signature, expected):
            return None
        return int(channel_text)
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def csrf_token(secret: str, action: str, on_date: date | None = None) -> str:
    if not secret:
        return ""
    day = (on_date or utc_now().date()).isoformat()
    payload = f"csrf|{action}|{day}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_csrf(secret: str, action: str, supplied: str) -> bool:
    if not secret or not supplied:
        return False
    today = utc_now().date()
    return any(
        hmac.compare_digest(csrf_token(secret, action, today - timedelta(days=days)), supplied)
        for days in (0, 1)
    )


class NotificationStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=15)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=15000")
        self.connection.executescript(SCHEMA)
        self.connection.execute("PRAGMA optimize")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "NotificationStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _create_subscriber(self, display_name: str, now: str) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO subscribers (public_id, display_name, status, created_at, updated_at)
            VALUES (?, ?, 'active', ?, ?)
            """,
            (secrets.token_urlsafe(18), display_name.strip()[:120], now, now),
        )
        return int(cursor.lastrowid)

    def _add_search(self, subscriber_id: int, search: SearchSpec, now: str) -> int:
        self.connection.execute(
            """
            INSERT INTO saved_searches (
                subscriber_id, city, area, max_rent, min_size, registration,
                particularity, active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT DO UPDATE SET active = 1, updated_at = excluded.updated_at
            """,
            (
                subscriber_id,
                search.city,
                search.area,
                search.max_rent,
                search.min_size,
                search.registration,
                search.particularity,
                now,
                now,
            ),
        )
        row = self.connection.execute(
            """
            SELECT id FROM saved_searches
            WHERE subscriber_id = ? AND city = ? AND area = ?
              AND IFNULL(max_rent, -1) = IFNULL(?, -1)
              AND IFNULL(min_size, -1) = IFNULL(?, -1)
              AND registration = ? AND particularity = ?
            """,
            (
                subscriber_id,
                search.city,
                search.area,
                search.max_rent,
                search.min_size,
                search.registration,
                search.particularity,
            ),
        ).fetchone()
        return int(row[0])

    def create_email_subscription(
        self, email: str, search: SearchSpec, display_name: str = ""
    ) -> tuple[ChannelRecord, str | None]:
        destination = validate_email(email)
        fingerprint = destination_fingerprint("email", destination)
        now = utc_now().isoformat()
        raw_token = secrets.token_urlsafe(32)
        with self.connection:
            existing = self.connection.execute(
                """
                SELECT nc.*, s.display_name
                FROM notification_channels AS nc
                JOIN subscribers AS s ON s.id = nc.subscriber_id
                WHERE nc.channel_type = 'email' AND nc.destination_fingerprint = ?
                """,
                (fingerprint,),
            ).fetchone()
            if existing is None:
                subscriber_id = self._create_subscriber(display_name, now)
                cursor = self.connection.execute(
                    """
                    INSERT INTO notification_channels (
                        subscriber_id, channel_type, destination,
                        destination_fingerprint, status, verification_token_hash,
                        created_at, updated_at
                    ) VALUES (?, 'email', ?, ?, 'pending', ?, ?, ?)
                    """,
                    (subscriber_id, destination, fingerprint, token_hash(raw_token), now, now),
                )
                channel_id = int(cursor.lastrowid)
                verification_token: str | None = raw_token
            else:
                subscriber_id = int(existing["subscriber_id"])
                channel_id = int(existing["id"])
                if existing["status"] == "active":
                    verification_token = None
                else:
                    verification_token = raw_token
                    self.connection.execute(
                        """
                        UPDATE notification_channels
                        SET destination = ?, status = 'pending',
                            verification_token_hash = ?, unsubscribed_at = NULL,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (destination, token_hash(raw_token), now, channel_id),
                    )
                if display_name.strip():
                    self.connection.execute(
                        "UPDATE subscribers SET display_name = ?, updated_at = ? WHERE id = ?",
                        (display_name.strip()[:120], now, subscriber_id),
                    )
            self._add_search(subscriber_id, search, now)
            self._record_event("subscription_saved", "channel", str(channel_id), {"type": "email"})
        return self.get_channel(channel_id), verification_token

    def create_telegram_subscription(
        self, search: SearchSpec, display_name: str = ""
    ) -> tuple[ChannelRecord, str]:
        now = utc_now().isoformat()
        raw_token = secrets.token_urlsafe(24)
        with self.connection:
            subscriber_id = self._create_subscriber(display_name, now)
            cursor = self.connection.execute(
                """
                INSERT INTO notification_channels (
                    subscriber_id, channel_type, status, verification_token_hash,
                    created_at, updated_at
                ) VALUES (?, 'telegram', 'pending', ?, ?, ?)
                """,
                (subscriber_id, token_hash(raw_token), now, now),
            )
            channel_id = int(cursor.lastrowid)
            self._add_search(subscriber_id, search, now)
            self._record_event(
                "subscription_saved", "channel", str(channel_id), {"type": "telegram"}
            )
        return self.get_channel(channel_id), raw_token

    def verify_email(self, raw_token: str) -> ChannelRecord | None:
        return self._verify_channel(raw_token, "email", None, None)

    def verify_telegram(
        self, raw_token: str, chat_id: str, display_destination: str
    ) -> ChannelRecord | None:
        fingerprint = destination_fingerprint("telegram", chat_id)
        return self._verify_channel(raw_token, "telegram", chat_id, fingerprint, display_destination)

    def _verify_channel(
        self,
        raw_token: str,
        channel_type: str,
        destination: str | None,
        fingerprint: str | None,
        display_destination: str | None = None,
    ) -> ChannelRecord | None:
        now = utc_now().isoformat()
        with self.connection:
            row = self.connection.execute(
                """
                SELECT id, subscriber_id FROM notification_channels
                WHERE channel_type = ? AND verification_token_hash = ?
                  AND status = 'pending'
                """,
                (channel_type, token_hash(raw_token)),
            ).fetchone()
            if row is None:
                return None
            channel_id = int(row["id"])
            pending_subscriber_id = int(row["subscriber_id"])

            if channel_type == "telegram" and fingerprint:
                existing = self.connection.execute(
                    """
                    SELECT id, subscriber_id FROM notification_channels
                    WHERE channel_type = 'telegram' AND destination_fingerprint = ?
                      AND id != ?
                    """,
                    (fingerprint, channel_id),
                ).fetchone()
                if existing is not None:
                    existing_channel_id = int(existing["id"])
                    existing_subscriber_id = int(existing["subscriber_id"])
                    searches = self.connection.execute(
                        "SELECT * FROM saved_searches WHERE subscriber_id = ?",
                        (pending_subscriber_id,),
                    ).fetchall()
                    for search in searches:
                        self._add_search(
                            existing_subscriber_id,
                            SearchSpec(
                                city=str(search["city"]),
                                area=str(search["area"]),
                                max_rent=search["max_rent"],
                                min_size=search["min_size"],
                                registration=str(search["registration"]),
                                particularity=str(search["particularity"]),
                            ),
                            now,
                        )
                    self.connection.execute(
                        """
                        UPDATE notification_channels
                        SET destination = ?, status = 'active', unsubscribed_at = NULL,
                            verified_at = COALESCE(verified_at, ?), updated_at = ?
                        WHERE id = ?
                        """,
                        (destination, now, now, existing_channel_id),
                    )
                    if display_destination:
                        self.connection.execute(
                            """
                            UPDATE subscribers
                            SET display_name = COALESCE(NULLIF(display_name, ''), ?),
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (display_destination[:120], now, existing_subscriber_id),
                        )
                    self.connection.execute(
                        "DELETE FROM subscribers WHERE id = ?",
                        (pending_subscriber_id,),
                    )
                    channel_id = existing_channel_id
                    self._record_event(
                        "subscription_merged",
                        "channel",
                        str(channel_id),
                        {"type": "telegram"},
                    )
                    return self.get_channel(channel_id)
            try:
                self.connection.execute(
                    """
                    UPDATE notification_channels
                    SET destination = COALESCE(?, destination),
                        destination_fingerprint = COALESCE(?, destination_fingerprint),
                        status = 'active', verification_token_hash = NULL,
                        verified_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (destination, fingerprint, now, now, channel_id),
                )
            except sqlite3.IntegrityError:
                return None
            if display_destination:
                self.connection.execute(
                    """
                    UPDATE subscribers SET display_name = COALESCE(NULLIF(display_name, ''), ?),
                        updated_at = ?
                    WHERE id = (SELECT subscriber_id FROM notification_channels WHERE id = ?)
                    """,
                    (display_destination[:120], now, channel_id),
                )
            self._record_event("subscription_verified", "channel", str(channel_id), {})
        return self.get_channel(channel_id)

    def get_channel(self, channel_id: int) -> ChannelRecord:
        row = self.connection.execute(
            """
            SELECT nc.*, s.display_name
            FROM notification_channels AS nc
            JOIN subscribers AS s ON s.id = nc.subscriber_id
            WHERE nc.id = ?
            """,
            (channel_id,),
        ).fetchone()
        if row is None:
            raise KeyError(channel_id)
        return ChannelRecord(
            id=int(row["id"]),
            subscriber_id=int(row["subscriber_id"]),
            channel_type=str(row["channel_type"]),
            destination=str(row["destination"] or ""),
            status=str(row["status"]),
            verified_at=row["verified_at"],
            display_name=str(row["display_name"] or ""),
            created_at=str(row["created_at"]),
        )

    def channel_by_destination(self, channel_type: str, destination: str) -> ChannelRecord | None:
        fingerprint = destination_fingerprint(channel_type, destination)
        row = self.connection.execute(
            "SELECT id FROM notification_channels WHERE channel_type = ? AND destination_fingerprint = ?",
            (channel_type, fingerprint),
        ).fetchone()
        return self.get_channel(int(row[0])) if row else None

    def set_channel_status(self, channel_id: int, status: str) -> bool:
        if status not in VALID_CHANNEL_STATUSES:
            raise ValueError("Invalid channel status")
        now = utc_now().isoformat()
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE notification_channels
                SET status = ?, unsubscribed_at = CASE WHEN ? = 'unsubscribed' THEN ? ELSE NULL END,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, status, now, now, channel_id),
            )
            if cursor.rowcount:
                self._record_event(
                    "channel_status_changed", "channel", str(channel_id), {"status": status}
                )
            return bool(cursor.rowcount)

    def list_searches(self, subscriber_id: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT * FROM saved_searches
            WHERE subscriber_id = ?
            ORDER BY active DESC, created_at
            """,
            (subscriber_id,),
        ).fetchall()

    def set_search_active(self, search_id: int, subscriber_id: int, active: bool) -> bool:
        now = utc_now().isoformat()
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE saved_searches SET active = ?, updated_at = ?
                WHERE id = ? AND subscriber_id = ?
                """,
                (int(active), now, search_id, subscriber_id),
            )
            return bool(cursor.rowcount)

    def active_channels(self) -> list[ChannelRecord]:
        rows = self.connection.execute(
            """
            SELECT nc.id
            FROM notification_channels AS nc
            WHERE nc.status = 'active' AND nc.destination IS NOT NULL
            ORDER BY nc.id
            """
        ).fetchall()
        return [self.get_channel(int(row[0])) for row in rows]

    def active_search_specs(self, subscriber_id: int) -> list[SearchSpec]:
        return [
            SearchSpec(
                city=str(row["city"]),
                area=str(row["area"]),
                max_rent=row["max_rent"],
                min_size=row["min_size"],
                registration=str(row["registration"]),
                particularity=str(row["particularity"]),
            )
            for row in self.connection.execute(
                """
                SELECT * FROM saved_searches
                WHERE subscriber_id = ? AND active = 1
                ORDER BY id
                """,
                (subscriber_id,),
            )
        ]

    def last_successful_delivery(self, channel_id: int) -> str | None:
        row = self.connection.execute(
            """
            SELECT sent_at FROM deliveries
            WHERE channel_id = ? AND status IN ('sent', 'delivered')
            ORDER BY sent_at DESC LIMIT 1
            """,
            (channel_id,),
        ).fetchone()
        return str(row[0]) if row and row[0] else None

    def create_digest_run(self, local_date: str) -> int:
        started_at = utc_now().isoformat()
        cursor = self.connection.execute(
            """
            INSERT INTO digest_runs (run_key, local_date, started_at, status)
            VALUES (?, ?, ?, 'running')
            """,
            (f"daily:{local_date}:{secrets.token_urlsafe(8)}", local_date, started_at),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def begin_delivery(self, run_id: int, channel_id: int, local_date: str) -> int | None:
        now = utc_now().isoformat()
        with self.connection:
            existing = self.connection.execute(
                "SELECT id, status FROM deliveries WHERE channel_id = ? AND local_date = ?",
                (channel_id, local_date),
            ).fetchone()
            if existing and existing["status"] in {"sent", "delivered", "skipped"}:
                return None
            if existing:
                delivery_id = int(existing["id"])
                self.connection.execute(
                    """
                    UPDATE deliveries SET digest_run_id = ?, status = 'sending',
                        error_summary = '', created_at = ? WHERE id = ?
                    """,
                    (run_id, now, delivery_id),
                )
                self.connection.execute(
                    "DELETE FROM delivery_items WHERE delivery_id = ?", (delivery_id,)
                )
                return delivery_id
            cursor = self.connection.execute(
                """
                INSERT INTO deliveries (
                    digest_run_id, channel_id, local_date, status, created_at
                ) VALUES (?, ?, ?, 'sending', ?)
                """,
                (run_id, channel_id, local_date, now),
            )
            return int(cursor.lastrowid)

    def finish_delivery(
        self,
        delivery_id: int,
        status: str,
        listing_keys: list[str],
        provider_message_id: str = "",
        error_summary: str = "",
    ) -> None:
        sent_at = utc_now().isoformat() if status == "sent" else None
        with self.connection:
            self.connection.execute(
                """
                UPDATE deliveries
                SET status = ?, provider_message_id = ?, error_summary = ?, sent_at = ?
                WHERE id = ?
                """,
                (status, provider_message_id or None, error_summary[:500], sent_at, delivery_id),
            )
            self.connection.executemany(
                "INSERT OR IGNORE INTO delivery_items (delivery_id, listing_key) VALUES (?, ?)",
                [(delivery_id, key) for key in listing_keys],
            )

    def finish_digest_run(
        self,
        run_id: int,
        *,
        channel_count: int,
        sent_count: int,
        skipped_count: int,
        failed_count: int,
        error_summary: str = "",
    ) -> None:
        status = "success" if failed_count == 0 else "partial_failure"
        with self.connection:
            self.connection.execute(
                """
                UPDATE digest_runs
                SET finished_at = ?, status = ?, channel_count = ?, sent_count = ?,
                    skipped_count = ?, failed_count = ?, error_summary = ?
                WHERE id = ?
                """,
                (
                    utc_now().isoformat(),
                    status,
                    channel_count,
                    sent_count,
                    skipped_count,
                    failed_count,
                    error_summary[:500],
                    run_id,
                ),
            )

    def record_email_event(self, event_type: str, provider_message_id: str) -> bool:
        if not provider_message_id:
            return False
        delivery_status = {
            "email.delivered": "delivered",
            "email.bounced": "bounced",
            "email.complained": "complained",
        }.get(event_type)
        if delivery_status is None:
            return False
        now = utc_now().isoformat()
        with self.connection:
            row = self.connection.execute(
                "SELECT id, channel_id FROM deliveries WHERE provider_message_id = ?",
                (provider_message_id,),
            ).fetchone()
            if row is None:
                return False
            self.connection.execute(
                "UPDATE deliveries SET status = ? WHERE id = ?",
                (delivery_status, int(row["id"])),
            )
            if delivery_status in {"bounced", "complained"}:
                self.connection.execute(
                    """
                    UPDATE notification_channels
                    SET status = 'bounced', updated_at = ? WHERE id = ?
                    """,
                    (now, int(row["channel_id"])),
                )
            self._record_event(
                "email_event",
                "delivery",
                str(row["id"]),
                {"event": event_type},
            )
        return True

    def admin_snapshot(self) -> dict[str, Any]:
        channel_counts = {
            str(row["status"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT status, COUNT(*) AS count FROM notification_channels GROUP BY status"
            )
        }
        channels = [
            {
                "id": int(row["id"]),
                "type": str(row["channel_type"]),
                "destination": mask_destination(row["channel_type"], row["destination"]),
                "status": str(row["status"]),
                "display_name": str(row["display_name"] or ""),
                "verified_at": row["verified_at"],
                "search_count": int(row["search_count"]),
                "last_sent_at": row["last_sent_at"],
            }
            for row in self.connection.execute(
                """
                SELECT nc.*, s.display_name,
                       (SELECT COUNT(*) FROM saved_searches ss
                        WHERE ss.subscriber_id = nc.subscriber_id AND ss.active = 1) AS search_count,
                       (SELECT MAX(d.sent_at) FROM deliveries d
                        WHERE d.channel_id = nc.id AND d.status IN ('sent', 'delivered')) AS last_sent_at
                FROM notification_channels nc
                JOIN subscribers s ON s.id = nc.subscriber_id
                ORDER BY nc.created_at DESC
                LIMIT 100
                """
            )
        ]
        deliveries = [dict(row) for row in self.connection.execute(
            """
            SELECT d.id, d.local_date, d.status, d.sent_at, d.error_summary,
                   nc.channel_type
            FROM deliveries d
            JOIN notification_channels nc ON nc.id = d.channel_id
            ORDER BY d.created_at DESC LIMIT 50
            """
        )]
        jobs = [dict(row) for row in self.connection.execute(
            """
            SELECT id, job_name, started_at, finished_at, status,
                   details_json, error_summary
            FROM job_runs ORDER BY started_at DESC LIMIT 50
            """
        )]
        digests = [dict(row) for row in self.connection.execute(
            "SELECT * FROM digest_runs ORDER BY started_at DESC LIMIT 30"
        )]
        listing_stats = {
            str(row["source_city"]): int(row["count"])
            for row in self.connection.execute(
                """
                SELECT r.source_city, COUNT(*) AS count
                FROM listings l JOIN raw_posts r ON r.dedupe_key = l.raw_post_key
                WHERE l.listing_kind = 'offer'
                GROUP BY r.source_city
                """
            )
        }
        return {
            "channel_counts": channel_counts,
            "channels": channels,
            "deliveries": deliveries,
            "jobs": jobs,
            "digests": digests,
            "listing_stats": listing_stats,
        }

    def _record_event(
        self, event_type: str, subject_type: str, subject_id: str, details: dict[str, Any]
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO admin_events (
                event_type, subject_type, subject_id, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                event_type,
                subject_type,
                subject_id,
                json.dumps(details, ensure_ascii=False),
                utc_now().isoformat(),
            ),
        )


def start_job(path: Path, job_name: str) -> int:
    with NotificationStore(path) as store:
        cursor = store.connection.execute(
            """
            INSERT INTO job_runs (job_name, started_at, status)
            VALUES (?, ?, 'running')
            """,
            (job_name, utc_now().isoformat()),
        )
        store.connection.commit()
        return int(cursor.lastrowid)


def finish_job(
    path: Path,
    job_id: int,
    status: str,
    details: dict[str, Any] | None = None,
    error_summary: str = "",
) -> None:
    with NotificationStore(path) as store:
        with store.connection:
            store.connection.execute(
                """
                UPDATE job_runs
                SET finished_at = ?, status = ?, details_json = ?, error_summary = ?
                WHERE id = ?
                """,
                (
                    utc_now().isoformat(),
                    status,
                    json.dumps(details or {}, ensure_ascii=False),
                    error_summary[:500],
                    job_id,
                ),
            )


@contextmanager
def tracked_job(path: Path, job_name: str) -> Iterator[dict[str, Any]]:
    job_id = start_job(path, job_name)
    details: dict[str, Any] = {}
    try:
        yield details
    except Exception as exc:
        finish_job(path, job_id, "failed", details, f"{type(exc).__name__}: {exc}")
        raise
    else:
        finish_job(path, job_id, "success", details)


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Chineur2000/1.0 (+https://facebookrooms.nl)",
            **headers,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Provider HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Provider request failed: {exc}") from exc


def send_resend_email(
    settings: NotificationSettings,
    *,
    to: str,
    subject: str,
    html_body: str,
    text_body: str,
    unsubscribe_url: str | None = None,
) -> str:
    if not settings.email_enabled:
        raise RuntimeError("Email delivery is not configured.")
    payload: dict[str, Any] = {
        "from": settings.resend_from,
        "to": [to],
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }
    if unsubscribe_url:
        payload["headers"] = {
            "List-Unsubscribe": f"<{unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
    response = _post_json(
        "https://api.resend.com/emails",
        {"Authorization": f"Bearer {settings.resend_api_key}"},
        payload,
    )
    message_id = response.get("id")
    if not message_id:
        raise RuntimeError("Email provider returned no message id.")
    return str(message_id)


def send_telegram_message(
    settings: NotificationSettings,
    *,
    chat_id: str,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> str:
    if not settings.telegram_enabled:
        raise RuntimeError("Telegram delivery is not configured.")
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    response = _post_json(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
        {},
        payload,
    )
    if not response.get("ok"):
        raise RuntimeError(str(response.get("description") or "Telegram send failed."))
    message_id = (response.get("result") or {}).get("message_id")
    return str(message_id or "")


EmailSender = Callable[..., str]
TelegramSender = Callable[..., str]
