from __future__ import annotations

import ipaddress
import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from fastapi import Request

from fb_automator.storage import SCHEMA


GEO_CACHE_DAYS = 90


def client_ip(request: Request) -> str | None:
    """Return the real client IP, trusting forwarded headers only from localhost."""
    peer = _parse_ip(request.client.host if request.client else "")
    if peer is None:
        return None
    if peer.is_loopback:
        forwarded = request.headers.get("x-forwarded-for", "")
        for value in reversed(forwarded.split(",")):
            candidate = _parse_ip(value.strip())
            if candidate is not None:
                return candidate.compressed
    return peer.compressed


def should_track_visit(request: Request, status_code: int) -> bool:
    if request.method not in {"GET", "HEAD"} or status_code >= 400:
        return False
    path = request.url.path
    return path == "/" or path.startswith("/ville/")


def record_page_view(
    database: Path,
    *,
    ip_address: str,
    path: str,
    referrer: str = "",
    user_agent: str = "",
    now: datetime | None = None,
    geo_lookup: Callable[[str], dict[str, str] | None] | None = None,
) -> None:
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    lookup_cutoff = (observed_at - timedelta(days=GEO_CACHE_DAYS)).isoformat()
    database.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(database, timeout=15) as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        cached = connection.execute(
            "SELECT * FROM visitor_geo WHERE ip_address = ? AND looked_up_at >= ?",
            (ip_address, lookup_cutoff),
        ).fetchone()
        geo = dict(cached) if cached is not None else {}
        cursor = connection.execute(
            """
            INSERT INTO page_views (
                visited_at, ip_address, path, referrer, user_agent,
                city, region, country, country_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observed_at.isoformat(),
                ip_address,
                path[:500],
                referrer[:500],
                user_agent[:700],
                geo.get("city", ""),
                geo.get("region", ""),
                geo.get("country", ""),
                geo.get("country_code", ""),
            ),
        )
        view_id = int(cursor.lastrowid)
        connection.commit()

    if cached is not None or not _is_public_ip(ip_address):
        return
    lookup = geo_lookup or lookup_ip_location
    resolved = lookup(ip_address)
    if not resolved:
        return

    clean = {
        key: str(resolved.get(key, ""))[:160]
        for key in ("city", "region", "country", "country_code", "provider")
    }
    with sqlite3.connect(database, timeout=15) as connection:
        connection.execute(
            """
            INSERT INTO visitor_geo (
                ip_address, city, region, country, country_code, provider, looked_up_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ip_address) DO UPDATE SET
                city = excluded.city,
                region = excluded.region,
                country = excluded.country,
                country_code = excluded.country_code,
                provider = excluded.provider,
                looked_up_at = excluded.looked_up_at
            """,
            (
                ip_address,
                clean["city"],
                clean["region"],
                clean["country"],
                clean["country_code"],
                clean["provider"],
                observed_at.isoformat(),
            ),
        )
        connection.execute(
            """
            UPDATE page_views
            SET city = ?, region = ?, country = ?, country_code = ?
            WHERE id = ?
            """,
            (
                clean["city"],
                clean["region"],
                clean["country"],
                clean["country_code"],
                view_id,
            ),
        )
        connection.commit()


def lookup_ip_location(ip_address: str) -> dict[str, str] | None:
    url = f"https://ipwho.is/{urllib.parse.quote(ip_address, safe=':')}"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "Chineur2000/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    if not isinstance(payload, dict) or payload.get("success") is False:
        return None
    connection = payload.get("connection") or {}
    return {
        "city": str(payload.get("city") or ""),
        "region": str(payload.get("region") or ""),
        "country": str(payload.get("country") or ""),
        "country_code": str(payload.get("country_code") or ""),
        "provider": str(connection.get("isp") or ""),
    }


def browser_label(user_agent: str) -> str:
    if not user_agent:
        return "Inconnu"
    if "Edg/" in user_agent:
        browser = "Edge"
    elif "Firefox/" in user_agent:
        browser = "Firefox"
    elif "Chrome/" in user_agent or "CriOS/" in user_agent:
        browser = "Chrome"
    elif "Safari/" in user_agent:
        browser = "Safari"
    else:
        browser = "Autre"

    if "iPhone" in user_agent or "iPad" in user_agent:
        device = "iOS"
    elif "Android" in user_agent:
        device = "Android"
    elif "Macintosh" in user_agent:
        device = "macOS"
    elif "Windows" in user_agent:
        device = "Windows"
    elif "Linux" in user_agent:
        device = "Linux"
    else:
        device = ""
    return " · ".join(item for item in (browser, device) if item)


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _is_public_ip(value: str) -> bool:
    parsed = _parse_ip(value)
    return bool(parsed and parsed.is_global)
