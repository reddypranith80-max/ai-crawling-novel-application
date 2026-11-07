"""Plain data models shared by crawlers, the engine and the exporters.

Crawlers only ever need to build :class:`NovelInfo` and :class:`Chapter`
objects; the engine takes care of numbering chapters, grouping volumes and
persisting everything to disk.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(slots=True)
class NovelInfo:
    """Metadata about a novel, as returned by ``BaseCrawler.get_info``."""

    title: str
    url: str = ""
    authors: list[str] = field(default_factory=list)
    cover_url: str | None = None
    #: Synopsis as an HTML fragment (plain text is fine too).
    synopsis: str = ""
    tags: list[str] = field(default_factory=list)
    language: str = "en"
    #: Free-form status such as ``"ongoing"`` or ``"completed"``.
    status: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NovelInfo:
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)


@dataclass(slots=True)
class Chapter:
    """One chapter of a novel.

    Crawlers fill in ``title``, ``url`` and optionally ``volume`` (a volume or
    arc *title*). The engine assigns ``id`` (1-based reading order) and
    ``volume_id``. ``body`` holds cleaned HTML once the chapter is downloaded
    and is never written to ``meta.json``.
    """

    title: str
    url: str
    volume: str | None = None
    id: int = 0
    volume_id: int = 0
    body: str | None = None

    def to_dict(self, *, with_body: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if not with_body:
            data.pop("body", None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Chapter:
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)


@dataclass(slots=True)
class Volume:
    """A group of consecutive chapters. Derived by the engine, never by crawlers."""

    id: int
    title: str
    chapter_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class Novel:
    """A novel with its full chapter list, as stored in ``meta.json``."""

    info: NovelInfo
    source: str
    chapters: list[Chapter] = field(default_factory=list)
    updated_at: str = field(default_factory=lambda: utc_now())

    # -- convenience -------------------------------------------------------
    @property
    def title(self) -> str:
        return self.info.title

    @property
    def url(self) -> str:
        return self.info.url

    @property
    def volumes(self) -> list[Volume]:
        """Volumes in reading order, built from ``Chapter.volume_id``."""
        volumes: dict[int, Volume] = {}
        for ch in self.chapters:
            vol = volumes.get(ch.volume_id)
            if vol is None:
                title = ch.volume or (f"Volume {ch.volume_id}" if ch.volume_id else "Chapters")
                vol = volumes[ch.volume_id] = Volume(ch.volume_id, title)
            vol.chapter_ids.append(ch.id)
        return list(volumes.values())

    def number_chapters(self) -> None:
        """Assign sequential chapter ids and volume ids (first-seen order)."""
        volume_ids: dict[str, int] = {}
        for index, ch in enumerate(self.chapters, start=1):
            ch.id = index
            if ch.volume:
                ch.volume_id = volume_ids.setdefault(ch.volume, len(volume_ids) + 1)
            else:
                ch.volume_id = 0

    # -- (de)serialisation --------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "info": asdict(self.info),
            "source": self.source,
            "updated_at": self.updated_at,
            "chapters": [ch.to_dict() for ch in self.chapters],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Novel:
        return cls(
            info=NovelInfo.from_dict(data["info"]),
            source=data.get("source", "unknown"),
            chapters=[Chapter.from_dict(c) for c in data.get("chapters", [])],
            updated_at=data.get("updated_at") or utc_now(),
        )


def utc_now() -> str:
    """Current UTC time as an ISO-8601 string without microseconds."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
