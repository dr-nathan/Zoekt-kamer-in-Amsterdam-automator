from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class GroupSource:
    name: str
    url: str


@dataclass(frozen=True, slots=True)
class RawPost:
    group_name: str
    group_url: str
    post_id: str | None
    post_url: str | None
    text: str
    published_label: str | None
    scraped_at: str
    reaction_count: int | None = None
    comment_count: int | None = None
    image_urls: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return asdict(self)
