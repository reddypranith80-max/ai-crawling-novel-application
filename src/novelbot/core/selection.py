"""Chapter range selection: ``--all``, ``--first``, ``--last``, ``--range``, ``--volumes``."""

from __future__ import annotations

import re
from dataclasses import dataclass

from novelbot.errors import NovelBotError
from novelbot.models import Chapter, Novel

_PART = re.compile(r"^\s*(\d*)\s*(?:(-|\.\.|:)\s*(\d*))?\s*$")


class SelectionError(NovelBotError):
    pass


def parse_ranges(spec: str, total: int) -> list[int]:
    """Parse ``"1-10, 15, 20-"`` into sorted 1-based ids within ``1..total``.

    ``"-5"`` means 1..5 and ``"20-"`` means 20..total. ``..`` and ``:`` are
    accepted as range separators too.
    """
    ids: set[int] = set()
    for part in spec.split(","):
        if not part.strip():
            continue
        match = _PART.match(part)
        if not match or (not match.group(1) and not match.group(3) and match.group(2)):
            raise SelectionError(f"Invalid chapter range: {part.strip()!r}")
        start_s, sep, end_s = match.groups()
        if not sep:
            if not start_s:
                raise SelectionError(f"Invalid chapter range: {part.strip()!r}")
            start = end = int(start_s)
        else:
            start = int(start_s) if start_s else 1
            end = int(end_s) if end_s else total
        if start > end:
            start, end = end, start
        ids.update(range(max(1, start), min(total, end) + 1))
    return sorted(ids)


@dataclass(slots=True)
class Selection:
    """What the user asked for. With no fields set, everything is selected."""

    all: bool = False
    first: int | None = None
    last: int | None = None
    ranges: str | None = None
    volumes: str | None = None

    @property
    def is_empty(self) -> bool:
        return not (self.all or self.first or self.last or self.ranges or self.volumes)

    def describe(self) -> str:
        if self.first:
            return f"first {self.first}"
        if self.last:
            return f"last {self.last}"
        if self.ranges:
            return f"chapters {self.ranges}"
        if self.volumes:
            return f"volumes {self.volumes}"
        return "all chapters"

    def upper_bound(self) -> int | None:
        """Highest chapter number needed, when known before the full list."""
        if self.first:
            return self.first
        if self.ranges and not self.ranges.strip().endswith("-"):
            numbers = [int(n) for n in re.findall(r"\d+", self.ranges)]
            return max(numbers) if numbers else None
        return None

    def apply(self, novel: Novel) -> list[Chapter]:
        chapters = novel.chapters
        options = sum(bool(x) for x in (self.first, self.last, self.ranges, self.volumes))
        if options > 1:
            raise SelectionError("Use only one of --first, --last, --range or --volumes")
        if (self.first is not None and self.first < 1) or (self.last is not None and self.last < 1):
            raise SelectionError("--first/--last must be at least 1")
        if self.all or self.is_empty:
            return list(chapters)
        if self.first:
            return chapters[: self.first]
        if self.last:
            return chapters[-self.last:]
        if self.ranges:
            wanted = set(parse_ranges(self.ranges, len(chapters)))
            picked = [ch for ch in chapters if ch.id in wanted]
            if not picked:
                raise SelectionError(f"No chapters match {self.ranges!r} "
                                     f"(the novel has {len(chapters)})")
            return picked
        volumes = novel.volumes
        wanted_vols = set(parse_ranges(self.volumes or "", len(volumes)))
        ids = {cid for i, vol in enumerate(volumes, 1) if i in wanted_vols
               for cid in vol.chapter_ids}
        picked = [ch for ch in chapters if ch.id in ids]
        if not picked:
            raise SelectionError(f"No volumes match {self.volumes!r} "
                                 f"(the novel has {len(volumes)})")
        return picked
