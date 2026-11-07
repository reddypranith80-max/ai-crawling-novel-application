"""Table-of-contents structure shared by the EPUB and PDF exporters.

* Chapters spanning several volumes are grouped by volume.
* Otherwise, long novels (e.g. 1000+ chapters) are split into blocks of
  ``group_size`` chapters ("Chapters 1-100", "Chapters 101-200", ...) so the
  reader's contents view stays navigable instead of one endless list.
* Short novels get a single flat list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from novelbot.models import Chapter


@dataclass(slots=True)
class TocSection:
    title: str | None
    kind: Literal["volume", "group", "flat"]
    chapters: list[Chapter] = field(default_factory=list)


def build_sections(chapters: list[Chapter], group_size: int = 100) -> list[TocSection]:
    if not chapters:
        return []
    if len({ch.volume_id for ch in chapters}) > 1:
        sections: list[TocSection] = []
        current_volume: int | None = None
        for ch in chapters:
            if ch.volume_id != current_volume:
                title = ch.volume or (f"Volume {ch.volume_id}" if ch.volume_id else "Chapters")
                sections.append(TocSection(title, "volume"))
                current_volume = ch.volume_id
            sections[-1].chapters.append(ch)
        return sections
    if group_size > 0 and len(chapters) > group_size:
        groups = []
        for start in range(0, len(chapters), group_size):
            block = chapters[start : start + group_size]
            title = f"Chapters {block[0].id}–{block[-1].id}"
            groups.append(TocSection(title, "group", block))
        return groups
    return [TocSection(None, "flat", list(chapters))]


def chapter_heading(chapter: Chapter) -> str:
    return chapter.title.strip() or f"Chapter {chapter.id}"
