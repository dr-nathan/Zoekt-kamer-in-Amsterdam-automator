from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from fb_automator.listing_models import AmsterdamNeighborhood, LausanneNeighborhood

DEFAULT_DATABASE = Path("data/listings.db")
ASSET_ROOT = Path(__file__).parent / "web_assets"
LISTING_MAX_AGE_DAYS = 14


@dataclass(frozen=True, slots=True)
class CityConfig:
    slug: str
    name: str
    currency: str
    neighborhoods: tuple[str, ...]


CITIES = {
    "amsterdam": CityConfig(
        slug="amsterdam",
        name="Amsterdam",
        currency="EUR",
        neighborhoods=tuple(item.value for item in AmsterdamNeighborhood),
    ),
    "lausanne": CityConfig(
        slug="lausanne",
        name="Lausanne",
        currency="CHF",
        neighborhoods=tuple(item.value for item in LausanneNeighborhood),
    ),
}


@dataclass(frozen=True, slots=True)
class ListingSearch:
    city: str = "lausanne"
    area: str = ""
    max_rent: int | None = None
    min_size: int | None = None
    registration: str = "any"
    particularity: str = ""
    sort: str = "newest"


@dataclass(frozen=True, slots=True)
class ListingCard:
    key: str
    source_hash: str
    location: str
    neighborhood: str | None
    monthly_rent: float | None
    currency: str
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
    first_seen_at: str
    reaction_count: int | None
    comment_count: int | None
    accent: int
    image_url: str | None
    image_urls: tuple[str, ...]
    image_quality_score: int | None

    @property
    def rent_label(self) -> str:
        if self.monthly_rent is None:
            return "Loyer non précisé"
        amount = f"{self.monthly_rent:,.0f}".replace(",", "’")
        currency = self.currency if self.currency in {"CHF", "EUR"} else ""
        return f"{currency} {amount} / mois".strip()

    @property
    def size_label(self) -> str:
        if self.room_size_m2 is None:
            return "Surface non précisée"
        return f"Chambre de {self.room_size_m2:g} m²"

    @property
    def availability_label(self) -> str:
        if not self.available_from:
            return "Disponibilité non précisée"
        start = _display_date(self.available_from)
        if self.available_to:
            return f"{start} – {_display_date(self.available_to)}"
        return f"Dès le {start}"

    @property
    def engagement_label(self) -> str | None:
        parts: list[str] = []
        if self.reaction_count is not None:
            suffix = "réaction" if self.reaction_count == 1 else "réactions"
            parts.append(f"{self.reaction_count} {suffix}")
        if self.comment_count is not None:
            suffix = "commentaire" if self.comment_count == 1 else "commentaires"
            parts.append(f"{self.comment_count} {suffix}")
        return " · ".join(parts) or None

    @property
    def posted_label(self) -> str:
        parsed = _parse_datetime(self.first_seen_at)
        if parsed is None:
            return "Collectée récemment"
        now = datetime.now(timezone.utc)
        days = max(0, (now.date() - parsed.date()).days)
        if days == 0:
            return "Collectée aujourd’hui"
        if days == 1:
            return "Collectée hier"
        return f"Collectée il y a {days} jours"

    @property
    def is_new(self) -> bool:
        parsed = _parse_datetime(self.first_seen_at)
        if parsed is None:
            return False
        return (datetime.now(timezone.utc) - parsed).total_seconds() < 48 * 3600


class ListingRepository:
    def __init__(self, database: Path, now: datetime | None = None):
        self.database = database
        self._now = (
            (lambda: now)
            if now is not None
            else (lambda: datetime.now(timezone.utc))
        )

    def search(self, filters: ListingSearch, limit: int = 60) -> list[ListingCard]:
        if not self.database.exists():
            return []

        city = CITIES.get(filters.city)
        if city is None:
            return []
        cutoff = (self._now() - timedelta(days=LISTING_MAX_AGE_DAYS)).isoformat()
        clauses = [
            "l.listing_kind = 'offer'",
            "r.source_city = ?",
            "r.first_seen_at >= ?",
        ]
        params: list[Any] = [city.slug, cutoff]
        if filters.area:
            clauses.append("l.neighborhood = ?")
            params.append(filters.area)
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
            "newest": "r.first_seen_at DESC",
            "rent_asc": "l.monthly_rent IS NULL, l.monthly_rent ASC, r.first_seen_at DESC",
            "rent_desc": "l.monthly_rent IS NULL, l.monthly_rent DESC, r.first_seen_at DESC",
            "popular": (
                "(COALESCE(r.reaction_count, 0) + 2 * COALESCE(r.comment_count, 0)) "
                "DESC, r.first_seen_at DESC"
            ),
        }.get(filters.sort, "r.first_seen_at DESC")

        image_expression = "NULL AS image_paths_json"
        try:
            with closing(sqlite3.connect(
                f"file:{self.database.resolve()}?mode=ro", uri=True
            )) as probe:
                has_images = probe.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'post_images'"
                ).fetchone()
            if has_images:
                image_expression = """
                    (SELECT json_group_array(
                         json_object('position', ordered.position, 'path', ordered.local_path)
                     )
                     FROM (
                         SELECT pi.position, pi.local_path
                         FROM post_images AS pi
                         WHERE pi.raw_post_key = l.raw_post_key
                           AND pi.local_path IS NOT NULL
                         ORDER BY pi.position
                     ) AS ordered) AS image_paths_json
                """
        except sqlite3.Error:
            pass

        query = f"""
            SELECT l.raw_post_key, l.source_hash, l.monthly_rent, l.currency,
                   l.utilities, l.room_size_m2,
                   l.location_text, l.city, l.neighborhood, l.available_from,
                   l.available_to, l.lease_type, l.registration, l.furnishing,
                   l.amenities_json, l.particularities_json, l.summary,
                   l.primary_image_position, l.image_quality_score,
                   l.image_review_version,
                   r.source_city, r.post_url, r.group_name, r.published_label,
                   r.first_seen_at,
                   r.reaction_count, r.comment_count, {image_expression}
            FROM listings AS l
            JOIN raw_posts AS r ON r.dedupe_key = l.raw_post_key
            WHERE {' AND '.join(clauses)}
            ORDER BY {ordering}
            LIMIT ?
        """
        params.append(limit * 4)
        try:
            with closing(sqlite3.connect(
                f"file:{self.database.resolve()}?mode=ro", uri=True
            )) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(query, params).fetchall()
        except sqlite3.Error:
            return []
        ordered_hashes: list[str] = []
        best_rows: dict[str, sqlite3.Row] = {}
        for row in rows:
            source_hash = row["source_hash"] or row["raw_post_key"]
            current = best_rows.get(source_hash)
            if current is None:
                ordered_hashes.append(source_hash)
                best_rows[source_hash] = row
                continue
            candidate_score = (
                row["image_quality_score"] or 0,
                len(_json_image_paths(row["image_paths_json"])),
                (row["reaction_count"] or 0) + 2 * (row["comment_count"] or 0),
            )
            current_score = (
                current["image_quality_score"] or 0,
                len(_json_image_paths(current["image_paths_json"])),
                (current["reaction_count"] or 0)
                + 2 * (current["comment_count"] or 0),
            )
            if candidate_score > current_score:
                best_rows[source_hash] = row
        return [self._to_card(best_rows[key]) for key in ordered_hashes[:limit]]

    def _to_card(self, row: sqlite3.Row) -> ListingCard:
        city = CITIES.get(row["source_city"], CITIES["lausanne"])
        location = row["location_text"] or row["neighborhood"] or row["city"] or city.name
        key = row["raw_post_key"]
        image_records = tuple(
            (position, url)
            for position, path in _json_image_paths(row["image_paths_json"])
            if (url := self._media_url(path)) is not None
        )
        primary_position = row["primary_image_position"]
        reviewed = bool(row["image_review_version"])
        if primary_position is not None and any(
            position == primary_position for position, _ in image_records
        ):
            images = tuple(
                url for position, url in image_records if position == primary_position
            ) + tuple(
                url for position, url in image_records if position != primary_position
            )
            cover = images[0]
        elif reviewed:
            images = tuple(url for _, url in image_records)
            cover = None
        else:
            images = tuple(url for _, url in image_records)
            cover = images[0] if images else None
        return ListingCard(
            key=key,
            source_hash=row["source_hash"] or key,
            location=location,
            neighborhood=row["neighborhood"],
            monthly_rent=row["monthly_rent"],
            currency=row["currency"] or city.currency,
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
            first_seen_at=row["first_seen_at"],
            reaction_count=row["reaction_count"],
            comment_count=row["comment_count"],
            accent=int(hashlib.sha256(key.encode()).hexdigest()[:2], 16) % 5,
            image_url=cover,
            image_urls=images,
            image_quality_score=row["image_quality_score"],
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

    def particularities(self, city_slug: str) -> list[str]:
        if not self.database.exists():
            return []
        cutoff = (self._now() - timedelta(days=LISTING_MAX_AGE_DAYS)).isoformat()
        try:
            with closing(sqlite3.connect(
                f"file:{self.database.resolve()}?mode=ro", uri=True
            )) as connection:
                rows = connection.execute(
                    """
                    SELECT l.particularities_json
                    FROM listings AS l
                    JOIN raw_posts AS r ON r.dedupe_key = l.raw_post_key
                    WHERE l.listing_kind = 'offer'
                      AND r.source_city = ?
                      AND r.first_seen_at >= ?
                    """,
                    (city_slug, cutoff),
                )
                labels = {
                    label.strip()
                    for row in rows
                    for label in _json_strings(row[0])
                    if label.strip()
                }
        except sqlite3.Error:
            return []
        return sorted(labels, key=str.casefold)

    def city_counts(self) -> dict[str, int]:
        counts = {slug: 0 for slug in CITIES}
        if not self.database.exists():
            return counts
        cutoff = (self._now() - timedelta(days=LISTING_MAX_AGE_DAYS)).isoformat()
        try:
            with closing(sqlite3.connect(
                f"file:{self.database.resolve()}?mode=ro", uri=True
            )) as connection:
                rows = connection.execute(
                    """
                    SELECT r.source_city,
                           COUNT(DISTINCT COALESCE(NULLIF(l.source_hash, ''), l.raw_post_key))
                    FROM listings AS l
                    JOIN raw_posts AS r ON r.dedupe_key = l.raw_post_key
                    WHERE l.listing_kind = 'offer' AND r.first_seen_at >= ?
                    GROUP BY r.source_city
                    """,
                    (cutoff,),
                )
            for city_slug, count in rows:
                if city_slug in counts:
                    counts[str(city_slug)] = int(count)
        except sqlite3.Error:
            pass
        return counts


def create_app(database: Path | None = None, now: datetime | None = None) -> FastAPI:
    database_path = database or Path(os.environ.get("FB_DATABASE", DEFAULT_DATABASE))
    repository = ListingRepository(database_path, now=now)
    templates = Environment(
        loader=FileSystemLoader(ASSET_ROOT / "templates"),
        autoescape=select_autoescape(["html", "xml"]),
    )

    application = FastAPI(
        title="Chineur2000",
        description="Flux privé d’annonces de logement à Amsterdam et Lausanne.",
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
    def city_picker(request: Request) -> HTMLResponse:
        counts = repository.city_counts()
        template = templates.get_template("cities.html")
        return HTMLResponse(
            template.render(
                request=request,
                cities=[
                    {"slug": city.slug, "name": city.name, "count": counts[city.slug]}
                    for city in CITIES.values()
                ],
                database_ready=database_path.exists(),
            )
        )

    @application.get("/ville/{city_slug}", response_class=HTMLResponse)
    def city_listings(
        city_slug: str,
        request: Request,
        area: str = Query(default="", max_length=80),
        max_rent: str = Query(default="", max_length=10),
        min_size: str = Query(default="", max_length=10),
        registration: str = Query(default="any", max_length=20),
        particularity: str = Query(default="", max_length=80),
        sort: str = Query(default="newest", max_length=20),
    ) -> HTMLResponse:
        city = CITIES.get(city_slug.casefold())
        if city is None:
            return HTMLResponse("Ville inconnue", status_code=404)
        filters = ListingSearch(
            city=city.slug,
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
                city=city,
                cities=tuple(CITIES.values()),
                particularities=repository.particularities(city.slug),
                neighborhoods=city.neighborhoods,
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
                max_age_days=LISTING_MAX_AGE_DAYS,
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


def _json_image_paths(value: str | None) -> tuple[tuple[int, str], ...]:
    if not value:
        return ()
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return ()
    records: list[tuple[int, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        position = item.get("position")
        path = item.get("path")
        if isinstance(position, int) and isinstance(path, str):
            records.append((position, path))
    return tuple(records)


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
    months = (
        "janv.", "févr.", "mars", "avr.", "mai", "juin",
        "juil.", "août", "sept.", "oct.", "nov.", "déc.",
    )
    return f"{parsed.day} {months[parsed.month - 1]}"


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
