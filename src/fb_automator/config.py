from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from fb_automator.models import GroupSource


def load_groups(path: Path) -> list[GroupSource]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(
            f"Group configuration not found: {path}. "
            "Copy config/groups.example.json to config/groups.json first."
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(payload, list) or not payload:
        raise ValueError("Group configuration must be a non-empty JSON list.")

    groups: list[GroupSource] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Group entry {index} must be an object.")
        name = str(item.get("name", "")).strip()
        url = str(item.get("url", "")).strip()
        city = str(item.get("city", "")).strip().casefold()
        parsed = urlparse(url)
        if not name:
            raise ValueError(f"Group entry {index} has no name.")
        if parsed.scheme != "https" or parsed.hostname not in {
            "facebook.com",
            "www.facebook.com",
        } or not parsed.path.startswith("/groups/"):
            raise ValueError(f"Group entry {index} is not a Facebook group URL: {url}")
        if city not in {"amsterdam", "lausanne"}:
            raise ValueError(
                f"Group entry {index} must use city 'amsterdam' or 'lausanne'."
            )
        groups.append(GroupSource(name=name, url=url, city=city))
    return groups
