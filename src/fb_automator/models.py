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

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)
