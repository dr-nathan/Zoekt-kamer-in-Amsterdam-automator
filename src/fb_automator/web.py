from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

DEFAULT_DATABASE = Path("data/listings.db")
ASSET_ROOT = Path(__file__).parent / "web_assets"


@dataclass(frozen=True, slots=True)
class ListingSearch:
    area: str = ""
    max_rent: int | None = None
    min_size: int | None = None
    registration: str = "any"
    particularity: str = ""
    sort: str = "newest"


@dataclass(frozen=True, slots=True)
class ListingCard:
    key: str
    location: str
    neighborhood: str | None
    monthly_rent: float | None
    room_size_m2: float | None
    available_from: str | None
    available_to: str | None
    utilities: str
    registration: str
    furnishing: str
    lease_type: str
    summary: str
    particularities: tuple[str, ...]
    amenities: tuple[str, ...]
    post_url: str | None
    group_name: str
    published_label: str | None
    last_seen_at: str
    reaction_count: int | None
    comment_count: int | None
    accent: int
    image_url: str | None

    @property
    def rent_label(self) -> str:
        if self.monthly_rent is None:
            return "Price on request"
        amount = f"{self.monthly_rent:,.0f}".replace(",", ".")
        return f"€{amount} / month"

    @property
    def size_label(self) -> str:
        if self.room_size_m2 is None:
            return "Size unknown"
        return f"{self.room_size_m2:g} m² room"

    @property
    def availability_label(self) -> str:
        if not self.available_from:
            return "Availability unknown"
        start = _display_date(self.available_from)
        if self.available_to:
            return f"{start} – {_display_date(self.available_to)}"
        return f"From {start}"

    @property
    def engagement_label(self) -> str | None:
        parts: list[str] = []
        if self.reaction_count is not None:
            parts.append(f"{self.reaction_count} reaction{'s' if self.reaction_count != 1 else ''}")
        if self.comment_count is not None:
            parts.append(f"{self.comment_count} comment{'s' if self.comment_count != 1 else ''}")
        return " · ".join(parts) or None

    @property
    def posted_label(self) -> str:
        parsed = _parse_datetime(self.last_seen_at)
        if parsed is None:
            return "Recently collected"
        now = datetime.now(timezone.utc)
        days = max(0, (now.date() - parsed.date()).days)
        if days == 0:
            return "Collected today"
        if days == 1:
            return "Collected yesterday"
        return f"Collected {days} days ago"

    @property
    def is_new(self) -> bool:
        parsed = _parse_datetime(self.last_seen_at)
        if parsed is None:
            return False
        return (datetime.now(timezone.utc) - parsed).total_seconds() < 48 * 3600


class ListingRepository:
    def __init__(self, database: Path):
        self.database = database

    def search(self, filters: ListingSearch, limit: int = 60) -> list[ListingCard]:
        if not self.database.exists():
            return []

        clauses = ["l.listing_kind = 'offer'"]
        params: list[Any] = []
        if filters.area:
            clauses.append(
                "lower(COALESCE(l.neighborhood, '') || ' ' || "
                "COALESCE(l.location_text, '') || ' ' || COALESCE(l.city, '')) LIKE ?"
            )
            params.append(f"%{filters.area.lower()}%")
        if filters.max_rent is not None:
            clauses.append("l.monthly_rent IS NOT NULL AND l.monthly_rent <= ?")
            params.append(filters.max_rent)
        if filters.min_size is not None:
            clauses.append("l.room_size_m2 IS NOT NULL AND l.room_size_m2 >= ?")
            params.append(filters.min_size)
        if filters.registration in {"allowed", "not_allowed", "required"}:
            clauses.append("l.registration = ?")
            params.append(filters.registration)
        if filters.particularity:
            clauses.append("lower(l.particularities_json) LIKE ?")
            params.append(f"%{filters.particularity.lower()}%")

        ordering = {
            "newest": "r.last_seen_at DESC",
            "rent_asc": "l.monthly_rent IS NULL, l.monthly_rent ASC, r.last_seen_at DESC",
            "rent_desc": "l.monthly_rent IS NULL, l.monthly_rent DESC, r.last_seen_at DESC",
            "popular": (
                "(COALESCE(r.reaction_count, 0) + 2 * COALESCE(r.comment_count, 0)) "
                "DESC, r.last_seen_at DESC"
            ),
        }.get(filters.sort, "r.last_seen_at DESC")

        image_expression = "NULL AS image_path"
        try:
            probe = sqlite3.connect(
                f"file:{self.database.resolve()}?mode=ro", uri=True
            )
            has_images = probe.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'post_images'"
            ).fetchone()
            probe.close()
            if has_images:
                image_expression = """
                    (SELECT pi.local_path FROM post_images AS pi
                     WHERE pi.raw_post_key = l.raw_post_key
                       AND pi.local_path IS NOT NULL
                     ORDER BY pi.position LIMIT 1) AS image_path
                """
        except sqlite3.Error:
            pass

        query = f"""
            SELECT l.raw_post_key, l.monthly_rent, l.utilities, l.room_size_m2,
                   l.location_text, l.city, l.neighborhood, l.available_from,
                   l.available_to, l.lease_type, l.registration, l.furnishing,
                   l.amenities_json, l.particularities_json, l.summary,
                   r.post_url, r.group_name, r.published_label, r.last_seen_at,
                   r.reaction_count, r.comment_count, {image_expression}
            FROM listings AS l
            JOIN raw_posts AS r ON r.dedupe_key = l.raw_post_key
            WHERE {' AND '.join(clauses)}
            ORDER BY {ordering}
            LIMIT ?
        """
        params.append(limit)
        try:
            connection = sqlite3.connect(
                f"file:{self.database.resolve()}?mode=ro", uri=True
            )
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, params).fetchall()
        except sqlite3.Error:
            return []
        finally:
            if "connection" in locals():
                connection.close()
        return [self._to_card(row) for row in rows]

    def _to_card(self, row: sqlite3.Row) -> ListingCard:
        location = row["location_text"] or row["neighborhood"] or row["city"] or "Amsterdam"
        key = row["raw_post_key"]
        return ListingCard(
            key=key,
            location=location,
            neighborhood=row["neighborhood"],
            monthly_rent=row["monthly_rent"],
            room_size_m2=row["room_size_m2"],
            available_from=row["available_from"],
            available_to=row["available_to"],
            utilities=row["utilities"],
            registration=row["registration"],
            furnishing=row["furnishing"],
            lease_type=row["lease_type"],
            summary=row["summary"],
            particularities=_json_strings(row["particularities_json"]),
            amenities=_json_strings(row["amenities_json"]),
            post_url=_safe_url(row["post_url"]),
            group_name=row["group_name"],
            published_label=row["published_label"],
            last_seen_at=row["last_seen_at"],
            reaction_count=row["reaction_count"],
            comment_count=row["comment_count"],
            accent=int(hashlib.sha256(key.encode()).hexdigest()[:2], 16) % 5,
            image_url=self._media_url(row["image_path"]),
        )

    def _media_url(self, local_path: str | None) -> str | None:
        if not local_path:
            return None
        path = Path(local_path)
        if path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
            return None
        if path.parts[0] != "images" or not (self.database.parent / path).is_file():
            return None
        return "/media/" + "/".join(quote(part) for part in path.parts[1:])

    def particularities(self) -> list[str]:
        if not self.database.exists():
            return []
        try:
            connection = sqlite3.connect(
                f"file:{self.database.resolve()}?mode=ro", uri=True
            )
            rows = connection.execute(
                "SELECT particularities_json FROM listings WHERE listing_kind = 'offer'"
            )
            labels = {
                label.strip()
                for row in rows
                for label in _json_strings(row[0])
                if label.strip()
            }
        except sqlite3.Error:
            return []
        finally:
            if "connection" in locals():
                connection.close()
        return sorted(labels, key=str.casefold)


def create_app(database: Path | None = None) -> FastAPI:
    database_path = database or Path(os.environ.get("FB_DATABASE", DEFAULT_DATABASE))
    repository = ListingRepository(database_path)
    templates = Environment(
        loader=FileSystemLoader(ASSET_ROOT / "templates"),
        autoescape=select_autoescape(["html", "xml"]),
    )

    application = FastAPI(
        title="Room Radar",
        description="Private browser for structured Facebook housing posts.",
        docs_url=None,
        redoc_url=None,
    )
    application.mount(
        "/static", StaticFiles(directory=ASSET_ROOT / "static"), name="static"
    )
    application.mount(
        "/media",
        StaticFiles(directory=database_path.parent / "images", check_dir=False),
        name="media",
    )

    @application.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        area: str = Query(default="", max_length=80),
        max_rent: str = Query(default="", max_length=10),
        min_size: str = Query(default="", max_length=10),
        registration: str = Query(default="any", max_length=20),
        particularity: str = Query(default="", max_length=80),
        sort: str = Query(default="newest", max_length=20),
    ) -> HTMLResponse:
        filters = ListingSearch(
            area=area.strip(),
            max_rent=_optional_int(max_rent, maximum=10_000),
            min_size=_optional_int(min_size, maximum=1_000),
            registration=registration,
            particularity=particularity,
            sort=sort,
        )
        cards = repository.search(filters)
        template = templates.get_template("index.html")
        return HTMLResponse(
            template.render(
                request=request,
                listings=cards,
                filters=filters,
                particularities=repository.particularities(),
                active_filter_count=sum(
                    [
                        bool(filters.area),
                        filters.max_rent is not None,
                        filters.min_size is not None,
                        filters.registration != "any",
                        bool(filters.particularity),
                    ]
                ),
                database_ready=database_path.exists(),
            )
        )

    @application.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok" if database_path.exists() else "waiting_for_database",
            "database": str(database_path),
        }

    return application


def _json_strings(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return ()
    return tuple(str(item) for item in parsed if isinstance(item, str))


def _safe_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    return None


def _display_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%d %b").lstrip("0")


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _optional_int(value: str, *, maximum: int) -> int | None:
    if not value.strip():
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if 0 <= parsed <= maximum else None


app = create_app()
