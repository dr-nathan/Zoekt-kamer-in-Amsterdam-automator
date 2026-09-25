from __future__ import annotations

import hashlib
import html
import hmac
import json
import os
import re
import shutil
import sqlite3
import urllib.parse
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from fb_automator.listing_models import AmsterdamNeighborhood, LausanneNeighborhood
from fb_automator.notifications import (
    NotificationSettings,
    NotificationStore,
    SearchSpec,
    csrf_token,
    send_resend_email,
    send_telegram_message,
    sign_action,
    verify_action,
    verify_csrf,
)

DEFAULT_DATABASE = Path("data/listings.db")
ASSET_ROOT = Path(__file__).parent / "web_assets"
ASSET_VERSION = "20260925-1"
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

UNKNOWN_LOCATION_VALUES = {
    "unknown",
    "unknown location",
    "inconnu",
    "lieu inconnu",
    "non précisé",
    "not specified",
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
    property_size_m2: float | None
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
        if self.room_size_m2 is not None:
            return f"Chambre de {self.room_size_m2:g} m²"
        if self.property_size_m2 is not None:
            return f"Logement de {self.property_size_m2:g} m²"
        return "Surface non précisée"

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

    def search(self, filters: ListingSearch, limit: int = 200) -> list[ListingCard]:
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
            clauses.append(
                "COALESCE(l.room_size_m2, l.property_size_m2) IS NOT NULL "
                "AND COALESCE(l.room_size_m2, l.property_size_m2) >= ?"
            )
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
                   l.utilities, l.room_size_m2, l.property_size_m2,
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
            if not _facebook_post_is_recent(
                row["published_label"], self._now(), LISTING_MAX_AGE_DAYS
            ):
                continue
            source_hash = row["source_hash"] or row["raw_post_key"]
            current = best_rows.get(source_hash)
            if current is None:
                ordered_hashes.append(source_hash)
                best_rows[source_hash] = row
                continue
            if _row_quality(row) > _row_quality(current):
                best_rows[source_hash] = row

        deduplicated: list[sqlite3.Row] = []
        for source_hash in ordered_hashes:
            row = best_rows[source_hash]
            duplicate_index = next(
                (
                    index
                    for index, existing in enumerate(deduplicated)
                    if _rows_are_near_duplicates(row, existing)
                ),
                None,
            )
            if duplicate_index is None:
                deduplicated.append(row)
            elif _row_quality(row) > _row_quality(deduplicated[duplicate_index]):
                deduplicated[duplicate_index] = row
        return [self._to_card(row) for row in deduplicated[:limit]]

    def _to_card(self, row: sqlite3.Row) -> ListingCard:
        city = CITIES.get(row["source_city"], CITIES["lausanne"])
        location = _display_location(row, city)
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
            currency=_display_currency(row["currency"], row["summary"], city),
            room_size_m2=row["room_size_m2"],
            property_size_m2=row["property_size_m2"],
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
        return {
            slug: len(self.search(ListingSearch(city=slug), limit=1_000))
            for slug in CITIES
        }


def create_app(database: Path | None = None, now: datetime | None = None) -> FastAPI:
    database_path = database or Path(os.environ.get("FB_DATABASE", DEFAULT_DATABASE))
    repository = ListingRepository(database_path, now=now)
    notification_settings = NotificationSettings.from_env()
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
                asset_version=ASSET_VERSION,
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
                asset_version=ASSET_VERSION,
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
                notifications_available=(
                    notification_settings.email_enabled
                    or notification_settings.telegram_enabled
                ),
                email_notifications_available=notification_settings.email_enabled,
                telegram_notifications_available=notification_settings.telegram_enabled,
                telegram_bot_username=notification_settings.telegram_bot_username,
                subscription_csrf=csrf_token(
                    notification_settings.app_secret, "subscribe"
                ),
            )
        )

    @application.post("/subscriptions/email", response_class=HTMLResponse)
    async def subscribe_email(request: Request) -> HTMLResponse:
        data = await _form_values(request)
        if not notification_settings.email_enabled:
            return _message_response(
                templates,
                "Notifications indisponibles",
                "L’envoi par e-mail n’est pas encore configuré.",
                status_code=503,
            )
        if not verify_csrf(
            notification_settings.app_secret, "subscribe", data.get("csrf", "")
        ):
            return _message_response(
                templates, "Lien expiré", "Rechargez la page et réessayez.", status_code=403
            )
        try:
            search = _search_spec_from_values(data)
            with NotificationStore(database_path) as store:
                channel, verification_token = store.create_email_subscription(
                    data.get("email", ""), search, data.get("display_name", "")
                )
            if verification_token is None:
                return _message_response(
                    templates,
                    "Filtres enregistrés",
                    "Cette adresse est déjà vérifiée. Les nouveaux filtres ont été ajoutés à son récapitulatif quotidien.",
                )
            verification_url = (
                f"{notification_settings.base_url}/subscriptions/confirm/{verification_token}"
            )
            safe_url = html.escape(verification_url, quote=True)
            send_resend_email(
                notification_settings,
                to=channel.destination,
                subject="Confirmez vos alertes Chineur2000",
                html_body=(
                    "<p>Confirmez votre adresse pour recevoir chaque matin les nouvelles annonces "
                    f"correspondant à vos filtres.</p><p><a href='{safe_url}'>"
                    "Confirmer mes alertes</a></p>"
                ),
                text_body=(
                    "Confirmez votre adresse pour recevoir les alertes Chineur2000 :\n"
                    f"{verification_url}\n"
                ),
            )
        except ValueError as exc:
            return _message_response(
                templates, "Informations invalides", str(exc), status_code=400
            )
        except RuntimeError:
            return _message_response(
                templates,
                "Adresse enregistrée",
                "L’adresse a été enregistrée, mais le message de vérification n’a pas pu partir. Réessayez dans quelques minutes.",
                status_code=502,
            )
        return _message_response(
            templates,
            "Vérifiez votre boîte mail",
            "Cliquez sur le lien reçu pour activer le récapitulatif quotidien de 09:00.",
        )

    @application.post("/subscriptions/telegram")
    async def subscribe_telegram(request: Request) -> HTMLResponse:
        data = await _form_values(request)
        if not notification_settings.telegram_enabled:
            return _message_response(
                templates,
                "Telegram indisponible",
                "Le bot Telegram n’est pas encore configuré.",
                status_code=503,
            )
        if not verify_csrf(
            notification_settings.app_secret, "subscribe", data.get("csrf", "")
        ):
            return _message_response(
                templates, "Lien expiré", "Rechargez la page et réessayez.", status_code=403
            )
        try:
            search = _search_spec_from_values(data)
            with NotificationStore(database_path) as store:
                _, verification_token = store.create_telegram_subscription(
                    search, data.get("display_name", "")
                )
        except ValueError as exc:
            return _message_response(
                templates, "Informations invalides", str(exc), status_code=400
            )
        bot_url = (
            f"https://t.me/{notification_settings.telegram_bot_username}"
            f"?start={urllib.parse.quote(verification_token)}"
        )
        return RedirectResponse(bot_url, status_code=303)

    @application.get("/subscriptions/confirm/{token}", response_class=HTMLResponse)
    def confirm_email(token: str) -> HTMLResponse:
        with NotificationStore(database_path) as store:
            channel = store.verify_email(token)
        if channel is None:
            return _message_response(
                templates,
                "Lien invalide",
                "Ce lien a déjà été utilisé ou n’est plus valable.",
                status_code=400,
            )
        return _message_response(
            templates,
            "Alertes activées",
            "Votre prochain récapitulatif sera envoyé à 09:00, heure d’Amsterdam.",
        )

    @application.get("/subscriptions/manage/{token}", response_class=HTMLResponse)
    def manage_subscription(token: str) -> HTMLResponse:
        channel_id = verify_action(notification_settings.app_secret, "manage", token)
        if channel_id is None:
            return _message_response(
                templates, "Lien invalide", "Ce lien de gestion n’est pas valable.", status_code=400
            )
        try:
            with NotificationStore(database_path) as store:
                channel = store.get_channel(channel_id)
                searches = store.list_searches(channel.subscriber_id)
        except KeyError:
            return _message_response(
                templates, "Abonnement introuvable", "Cet abonnement n’existe plus.", status_code=404
            )
        template = templates.get_template("manage.html")
        return HTMLResponse(
            template.render(channel=channel, searches=searches, token=token)
        )

    @application.post("/subscriptions/manage/{token}")
    async def update_subscription(token: str, request: Request):
        channel_id = verify_action(notification_settings.app_secret, "manage", token)
        if channel_id is None:
            return _message_response(
                templates, "Lien invalide", "Ce lien de gestion n’est pas valable.", status_code=400
            )
        data = await _form_values(request)
        try:
            with NotificationStore(database_path) as store:
                channel = store.get_channel(channel_id)
                action = data.get("action", "")
                if action == "pause":
                    store.set_channel_status(channel.id, "paused")
                elif action == "resume":
                    store.set_channel_status(channel.id, "active")
                elif action in {"enable_search", "disable_search"}:
                    search_id = int(data.get("search_id", "0"))
                    store.set_search_active(
                        search_id,
                        channel.subscriber_id,
                        action == "enable_search",
                    )
                else:
                    raise ValueError("Action inconnue")
        except (KeyError, ValueError):
            return _message_response(
                templates, "Action invalide", "La modification n’a pas été appliquée.", status_code=400
            )
        return RedirectResponse(f"/subscriptions/manage/{token}", status_code=303)

    @application.get("/unsubscribe/{token}", response_class=HTMLResponse)
    def unsubscribe_page(token: str) -> HTMLResponse:
        channel_id = verify_action(notification_settings.app_secret, "unsubscribe", token)
        if channel_id is None:
            return _message_response(
                templates, "Lien invalide", "Ce lien de désabonnement n’est pas valable.", status_code=400
            )
        try:
            with NotificationStore(database_path) as store:
                channel = store.get_channel(channel_id)
        except KeyError:
            return _message_response(
                templates, "Abonnement introuvable", "Cet abonnement n’existe plus.", status_code=404
            )
        template = templates.get_template("unsubscribe.html")
        return HTMLResponse(template.render(channel=channel, token=token, unsubscribed=False))

    @application.post("/unsubscribe/{token}", response_class=HTMLResponse)
    def unsubscribe(token: str) -> HTMLResponse:
        channel_id = verify_action(notification_settings.app_secret, "unsubscribe", token)
        if channel_id is None:
            return _message_response(
                templates, "Lien invalide", "Ce lien de désabonnement n’est pas valable.", status_code=400
            )
        with NotificationStore(database_path) as store:
            changed = store.set_channel_status(channel_id, "unsubscribed")
            channel = store.get_channel(channel_id) if changed else None
        if channel is None:
            return _message_response(
                templates, "Abonnement introuvable", "Cet abonnement n’existe plus.", status_code=404
            )
        template = templates.get_template("unsubscribe.html")
        return HTMLResponse(template.render(channel=channel, token=token, unsubscribed=True))

    @application.post("/webhooks/telegram")
    async def telegram_webhook(request: Request) -> JSONResponse:
        supplied_secret = request.headers.get("x-telegram-bot-api-secret-token", "")
        if (
            not notification_settings.telegram_enabled
            or not hmac.compare_digest(
                supplied_secret, notification_settings.telegram_webhook_secret
            )
        ):
            return JSONResponse({"ok": False}, status_code=403)
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"ok": False}, status_code=400)

        message = payload.get("message") or {}
        callback = payload.get("callback_query") or {}
        if callback:
            message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        if not chat_id:
            return JSONResponse({"ok": True})
        text = str(message.get("text") or "").strip()
        callback_data = str(callback.get("data") or "")
        username = str((message.get("from") or callback.get("from") or {}).get("username") or "")
        display = f"@{username}" if username else "Telegram"

        response_text = ""
        channel = None
        with NotificationStore(database_path) as store:
            if text.startswith("/start "):
                raw_token = text.split(maxsplit=1)[1]
                channel = store.verify_telegram(raw_token, chat_id, display)
                response_text = (
                    "Alertes Chineur2000 activées. Le récapitulatif arrive chaque jour à 09:00."
                    if channel
                    else "Ce lien d’activation est invalide ou a déjà été utilisé."
                )
            elif text.startswith("/stop") or callback_data == "unsubscribe":
                channel = store.channel_by_destination("telegram", chat_id)
                if channel:
                    store.set_channel_status(channel.id, "unsubscribed")
                response_text = "Les alertes Telegram ont été arrêtées."
            elif text.startswith("/settings"):
                channel = store.channel_by_destination("telegram", chat_id)
                response_text = (
                    "Utilisez le bouton ci-dessous pour gérer vos filtres."
                    if channel
                    else "Aucun abonnement actif n’est associé à cette conversation."
                )
            else:
                channel = store.channel_by_destination("telegram", chat_id)
                response_text = "Commandes disponibles : /settings et /stop."

        reply_markup = None
        if channel and channel.status != "unsubscribed":
            manage_token = sign_action(notification_settings.app_secret, "manage", channel.id)
            unsubscribe_token = sign_action(
                notification_settings.app_secret, "unsubscribe", channel.id
            )
            reply_markup = {
                "inline_keyboard": [
                    [
                        {
                            "text": "Gérer mes filtres",
                            "url": f"{notification_settings.base_url}/subscriptions/manage/{manage_token}",
                        }
                    ],
                    [
                        {
                            "text": "Se désabonner",
                            "url": f"{notification_settings.base_url}/unsubscribe/{unsubscribe_token}",
                        }
                    ],
                ]
            }
        try:
            send_telegram_message(
                notification_settings,
                chat_id=chat_id,
                text=response_text,
                reply_markup=reply_markup,
            )
        except RuntimeError:
            pass
        return JSONResponse({"ok": True})

    @application.post("/webhooks/resend")
    async def resend_webhook(request: Request) -> JSONResponse:
        if not notification_settings.resend_webhook_secret:
            return JSONResponse({"ok": False}, status_code=503)
        raw_body = await request.body()
        try:
            from svix.webhooks import Webhook, WebhookVerificationError

            event = Webhook(notification_settings.resend_webhook_secret).verify(
                raw_body, dict(request.headers)
            )
        except (WebhookVerificationError, ValueError):
            return JSONResponse({"ok": False}, status_code=403)
        event_type = str(event.get("type") or "")
        data = event.get("data") or {}
        provider_message_id = str(data.get("email_id") or data.get("id") or "")
        with NotificationStore(database_path) as store:
            recorded = store.record_email_event(event_type, provider_message_id)
        return JSONResponse({"ok": True, "recorded": recorded})

    @application.get("/admin", response_class=HTMLResponse)
    def admin_dashboard(request: Request) -> HTMLResponse:
        if request.headers.get("x-chineur-admin") != "1":
            return HTMLResponse("Introuvable", status_code=404)
        with NotificationStore(database_path) as store:
            snapshot = store.admin_snapshot()
        disk = shutil.disk_usage(database_path.parent)
        template = templates.get_template("admin.html")
        return HTMLResponse(
            template.render(
                snapshot=snapshot,
                disk_free_gb=round(disk.free / (1024**3), 1),
                providers={
                    "email": notification_settings.email_enabled,
                    "telegram": notification_settings.telegram_enabled,
                },
                csrf=csrf_token(notification_settings.app_secret, "admin"),
            )
        )

    @application.post("/admin/channels/{channel_id}")
    async def admin_update_channel(channel_id: int, request: Request):
        if request.headers.get("x-chineur-admin") != "1":
            return HTMLResponse("Introuvable", status_code=404)
        data = await _form_values(request)
        if not verify_csrf(
            notification_settings.app_secret, "admin", data.get("csrf", "")
        ):
            return HTMLResponse("Action expirée", status_code=403)
        action = data.get("action", "")
        status = {"pause": "paused", "resume": "active", "unsubscribe": "unsubscribed"}.get(
            action
        )
        if status is None:
            return HTMLResponse("Action invalide", status_code=400)
        with NotificationStore(database_path) as store:
            store.set_channel_status(channel_id, status)
        return RedirectResponse("/admin", status_code=303)

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


def _display_currency(
    stored_currency: str | None, summary: str, source_city: CityConfig
) -> str:
    mentions_chf = bool(re.search(r"\bCHF\b", summary, flags=re.IGNORECASE))
    mentions_eur = "€" in summary or bool(
        re.search(r"\bEUR\b", summary, flags=re.IGNORECASE)
    )
    if mentions_chf != mentions_eur:
        return "CHF" if mentions_chf else "EUR"
    if stored_currency in {"CHF", "EUR"}:
        return stored_currency
    return source_city.currency


def _row_quality(row: sqlite3.Row) -> tuple[int, int, int]:
    return (
        row["image_quality_score"] or 0,
        len(_json_image_paths(row["image_paths_json"])),
        (row["reaction_count"] or 0) + 2 * (row["comment_count"] or 0),
    )


def _rows_are_near_duplicates(left: sqlite3.Row, right: sqlite3.Row) -> bool:
    if left["source_city"] != right["source_city"]:
        return False
    left_rent = left["monthly_rent"]
    right_rent = right["monthly_rent"]
    if (left_rent is None) != (right_rent is None):
        return False
    if left_rent is not None and abs(left_rent - right_rent) > 1:
        return False
    left_location = _clean_location(left["location_text"])
    right_location = _clean_location(right["location_text"])
    if not left_location or not right_location:
        return False
    if left_location.casefold() != right_location.casefold():
        return False
    left_summary = " ".join(str(left["summary"]).casefold().split())
    right_summary = " ".join(str(right["summary"]).casefold().split())
    return SequenceMatcher(None, left_summary, right_summary).ratio() >= 0.84


def _facebook_post_is_recent(
    published_label: str | None, now: datetime, max_age_days: int
) -> bool:
    if not published_label:
        return True
    normalized = published_label.replace("\u202f", " ").replace("\xa0", " ")
    try:
        published = datetime.strptime(
            normalized, "%A, %B %d, %Y at %I:%M %p"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return published >= now - timedelta(days=max_age_days)


def _display_location(row: sqlite3.Row, source_city: CityConfig) -> str:
    location_text = _clean_location(row["location_text"])
    municipality = _clean_location(row["city"])
    neighborhood = _clean_location(row["neighborhood"])
    outside_label = f"hors {source_city.name}".casefold()

    if neighborhood and neighborhood.casefold() == outside_label:
        if municipality and municipality.casefold() not in {
            source_city.name.casefold(),
            outside_label,
        }:
            return municipality
        if location_text:
            return location_text
        return neighborhood

    return neighborhood or location_text or municipality or source_city.name


def _clean_location(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = " ".join(value.split())
    if cleaned.casefold() in UNKNOWN_LOCATION_VALUES:
        return None
    return cleaned


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


async def _form_values(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" not in content_type:
        return {}
    body = (await request.body()).decode("utf-8", errors="replace")
    return {
        key: values[-1]
        for key, values in urllib.parse.parse_qs(
            body, keep_blank_values=True, max_num_fields=30
        ).items()
    }


def _search_spec_from_values(values: dict[str, str]) -> SearchSpec:
    city_slug = values.get("city", "").strip().casefold()
    city = CITIES.get(city_slug)
    if city is None:
        raise ValueError("Ville invalide.")
    area = values.get("area", "").strip()
    if area and area not in city.neighborhoods:
        raise ValueError("Quartier invalide.")
    registration = values.get("registration", "any").strip()
    if registration not in {"any", "allowed", "required", "not_allowed"}:
        raise ValueError("Filtre de domiciliation invalide.")
    particularity = values.get("particularity", "").strip()[:80]
    return SearchSpec(
        city=city_slug,
        area=area,
        max_rent=_optional_int(values.get("max_rent", ""), maximum=10_000),
        min_size=_optional_int(values.get("min_size", ""), maximum=1_000),
        registration=registration,
        particularity=particularity,
    )


def _message_response(
    templates: Environment,
    title: str,
    message: str,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    template = templates.get_template("message.html")
    return HTMLResponse(
        template.render(title=title, message=message), status_code=status_code
    )


app = create_app()
