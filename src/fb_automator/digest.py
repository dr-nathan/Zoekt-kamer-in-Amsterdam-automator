from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from fb_automator.notifications import (
    AMSTERDAM_TIMEZONE,
    ChannelRecord,
    NotificationSettings,
    NotificationStore,
    SearchSpec,
    decode_filter_values,
    send_resend_email,
    send_telegram_message,
    sign_action,
)
from fb_automator.web import ListingCard, ListingRepository, ListingSearch, _parse_datetime


MAX_DIGEST_LISTINGS = 20
MAX_TELEGRAM_LISTINGS = 10


@dataclass(frozen=True, slots=True)
class DigestResult:
    channels: int
    sent: int
    skipped: int
    failed: int


def _matching_cards(
    repository: ListingRepository,
    searches: list[SearchSpec],
    since: datetime,
) -> list[ListingCard]:
    matches: dict[str, ListingCard] = {}
    for search in searches:
        areas = decode_filter_values(search.area) or ("",)
        particularities = decode_filter_values(search.particularity) or ("",)
        for area in areas:
            for particularity in particularities:
                cards = repository.search(
                    ListingSearch(
                        city=search.city,
                        area=area,
                        max_rent=search.max_rent,
                        min_size=search.min_size,
                        registration=search.registration,
                        particularity=particularity,
                        sort="newest",
                    ),
                    limit=500,
                )
                for card in cards:
                    seen = _parse_datetime(card.first_seen_at)
                    if seen is not None and seen >= since:
                        matches[card.key] = card
    return sorted(
        matches.values(),
        key=lambda item: _parse_datetime(item.first_seen_at) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )[:MAX_DIGEST_LISTINGS]


def _listing_line(card: ListingCard) -> str:
    details = [card.rent_label, card.location]
    if card.room_size_m2 is not None or card.property_size_m2 is not None:
        details.append(card.size_label)
    return " · ".join(details)


def _email_content(
    channel: ChannelRecord,
    cards: list[ListingCard],
    settings: NotificationSettings,
) -> tuple[str, str, str, str]:
    manage_token = sign_action(settings.app_secret, "manage", channel.id)
    unsubscribe_token = sign_action(settings.app_secret, "unsubscribe", channel.id)
    manage_url = f"{settings.base_url}/subscriptions/manage/{manage_token}"
    unsubscribe_url = f"{settings.base_url}/unsubscribe/{unsubscribe_token}"
    title = f"{len(cards)} nouvelle{'s' if len(cards) != 1 else ''} annonce{'s' if len(cards) != 1 else ''} Chineur2000"
    rows = []
    text_rows = []
    for card in cards:
        source_url = card.post_url or f"{settings.base_url}/ville/{card.neighborhood or ''}"
        rows.append(
            "<li style='margin:0 0 18px'>"
            f"<strong>{html.escape(_listing_line(card))}</strong><br>"
            f"<span>{html.escape(card.summary)}</span><br>"
            f"<a href='{html.escape(source_url, quote=True)}'>Voir sur Facebook</a>"
            "</li>"
        )
        text_rows.append(f"- {_listing_line(card)}\n  {card.summary}\n  {source_url}")
    greeting = f"Bonjour {html.escape(channel.display_name)}," if channel.display_name else "Bonjour,"
    html_body = (
        "<!doctype html><html><body style='font-family:Arial,sans-serif;color:#241b1d'>"
        f"<p>{greeting}</p>"
        f"<p>Voici les {len(cards)} nouvelles annonces correspondant à vos filtres.</p>"
        f"<ol>{''.join(rows)}</ol>"
        f"<p><a href='{manage_url}'>Gérer mes filtres</a> · "
        f"<a href='{unsubscribe_url}'>Se désabonner</a></p>"
        "<p style='color:#73686a;font-size:12px'>Vérifiez toujours l’annonce originale sur Facebook.</p>"
        "</body></html>"
    )
    text_body = (
        f"Bonjour{(' ' + channel.display_name) if channel.display_name else ''},\n\n"
        f"Voici les {len(cards)} nouvelles annonces correspondant à vos filtres.\n\n"
        + "\n\n".join(text_rows)
        + f"\n\nGérer mes filtres: {manage_url}\nSe désabonner: {unsubscribe_url}\n"
    )
    return title, html_body, text_body, unsubscribe_url


def _telegram_content(
    channel: ChannelRecord,
    cards: list[ListingCard],
    settings: NotificationSettings,
) -> tuple[str, dict[str, Any]]:
    manage_token = sign_action(settings.app_secret, "manage", channel.id)
    unsubscribe_token = sign_action(settings.app_secret, "unsubscribe", channel.id)
    manage_url = f"{settings.base_url}/subscriptions/manage/{manage_token}"
    unsubscribe_url = f"{settings.base_url}/unsubscribe/{unsubscribe_token}"
    total_count = len(cards)
    selected = cards[:MAX_TELEGRAM_LISTINGS]
    while True:
        lines = [
            f"Chineur2000 — {total_count} nouvelle{'s' if total_count != 1 else ''} annonce{'s' if total_count != 1 else ''}",
            "",
        ]
        for index, card in enumerate(selected, start=1):
            lines.extend(
                [
                    f"{index}. {_listing_line(card)}",
                    card.summary,
                    card.post_url or settings.base_url,
                    "",
                ]
            )
        if total_count > len(selected):
            lines.append(
                f"+ {total_count - len(selected)} autres annonces sur {settings.base_url}"
            )
        text = "\n".join(lines)
        if len(text) <= 3900 or len(selected) <= 1:
            break
        selected.pop()
    markup = {
        "inline_keyboard": [
            [{"text": "Gérer mes filtres", "url": manage_url}],
            [
                {"text": "Se désabonner", "url": unsubscribe_url},
                {"text": "Arrêter ici", "callback_data": "unsubscribe"},
            ],
        ]
    }
    return text, markup


def send_daily_digests(
    database: Path,
    *,
    settings: NotificationSettings | None = None,
    now: datetime | None = None,
    email_sender: Callable[..., str] = send_resend_email,
    telegram_sender: Callable[..., str] = send_telegram_message,
) -> DigestResult:
    selected_settings = settings or NotificationSettings.from_env()
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    local_date = current.astimezone(ZoneInfo(AMSTERDAM_TIMEZONE)).date().isoformat()
    repository = ListingRepository(database, now=current)
    sent_count = 0
    skipped_count = 0
    failed_count = 0
    errors: list[str] = []

    with NotificationStore(database) as store:
        channels = store.active_channels()
        run_id = store.create_digest_run(local_date)
        for channel in channels:
            delivery_id = store.begin_delivery(run_id, channel.id, local_date)
            if delivery_id is None:
                skipped_count += 1
                continue
            searches = store.active_search_specs(channel.subscriber_id)
            if not searches:
                store.finish_delivery(delivery_id, "skipped", [])
                skipped_count += 1
                continue
            since_text = store.last_successful_delivery(channel.id) or channel.verified_at or channel.created_at
            since = _parse_datetime(since_text) or current
            cards = _matching_cards(repository, searches, since)
            if not cards:
                store.finish_delivery(delivery_id, "skipped", [])
                skipped_count += 1
                continue
            keys = [card.key for card in cards]
            try:
                if channel.channel_type == "email":
                    subject, html_body, text_body, unsubscribe_url = _email_content(
                        channel, cards, selected_settings
                    )
                    provider_id = email_sender(
                        selected_settings,
                        to=channel.destination,
                        subject=subject,
                        html_body=html_body,
                        text_body=text_body,
                        unsubscribe_url=unsubscribe_url,
                    )
                elif channel.channel_type == "telegram":
                    text, reply_markup = _telegram_content(channel, cards, selected_settings)
                    provider_id = telegram_sender(
                        selected_settings,
                        chat_id=channel.destination,
                        text=text,
                        reply_markup=reply_markup,
                    )
                else:
                    raise RuntimeError(f"Unsupported channel: {channel.channel_type}")
            except Exception as exc:
                message = f"{channel.channel_type} channel {channel.id}: {type(exc).__name__}: {exc}"
                store.finish_delivery(delivery_id, "failed", keys, error_summary=message)
                errors.append(message)
                failed_count += 1
            else:
                store.finish_delivery(
                    delivery_id, "sent", keys, provider_message_id=provider_id
                )
                sent_count += 1
        store.finish_digest_run(
            run_id,
            channel_count=len(channels),
            sent_count=sent_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
            error_summary="; ".join(errors),
        )
    return DigestResult(
        channels=len(channels),
        sent=sent_count,
        skipped=skipped_count,
        failed=failed_count,
    )
