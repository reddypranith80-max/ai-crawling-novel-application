"""Concurrent chapter downloading with resume support.

Chapters already in the cache are skipped (unless ``force``). Each chapter
is fetched, cleaned, has its images localised and is then written to disk
immediately, so an interrupted run loses at most the chapters in flight.
A failing chapter is recorded and the rest continue.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Protocol

from novelbot.core.cleaner import ContentCleaner, has_text
from novelbot.core.images import ImageCache
from novelbot.core.storage import NovelStore
from novelbot.errors import BlockedError, EmptyChapterError, NovelBotError
from novelbot.models import Chapter
from novelbot.sources.base import BaseCrawler

log = logging.getLogger(__name__)


class ProgressHooks(Protocol):
    """Implemented by the CLI (rich progress bar); all methods optional-ish."""

    def start(self, total: int, description: str) -> None: ...
    def advance(self, chapter: Chapter, ok: bool) -> None: ...
    def finish(self) -> None: ...


class NullProgress:
    def start(self, total: int, description: str) -> None:
        pass

    def advance(self, chapter: Chapter, ok: bool) -> None:
        pass

    def finish(self) -> None:
        pass


@dataclass(slots=True)
class DownloadResult:
    requested: int = 0
    cached: int = 0
    downloaded: int = 0
    failed: list[tuple[Chapter, str]] = field(default_factory=list)
    #: Set when the site blocked us and the remaining chapters were skipped.
    aborted: str | None = None

    @property
    def ok(self) -> bool:
        return not self.failed and self.aborted is None


class ChapterDownloader:
    def __init__(
        self,
        crawler: BaseCrawler,
        store: NovelStore,
        cleaner: ContentCleaner,
        *,
        concurrency: int = 3,
        images: ImageCache | None = None,
        progress: ProgressHooks | None = None,
    ) -> None:
        self.crawler = crawler
        self.store = store
        self.cleaner = cleaner
        self.concurrency = max(1, concurrency)
        self.images = images
        self.progress = progress or NullProgress()

    async def run(self, chapters: list[Chapter], *, force: bool = False) -> DownloadResult:
        result = DownloadResult(requested=len(chapters))
        pending = chapters if force else [c for c in chapters if not self.store.has_chapter(c)]
        result.cached = len(chapters) - len(pending)
        if not pending:
            return result

        queue: asyncio.Queue[Chapter] = asyncio.Queue()
        for chapter in pending:
            queue.put_nowait(chapter)
        stop = asyncio.Event()

        async def worker() -> None:
            while not stop.is_set():
                try:
                    chapter = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    await self._download_one(chapter)
                    result.downloaded += 1
                    self.progress.advance(chapter, True)
                except BlockedError as exc:
                    result.aborted = str(exc)
                    stop.set()
                    self.progress.advance(chapter, False)
                except (NovelBotError, ValueError, KeyError, AttributeError, TypeError) as exc:
                    # Parser bugs on one odd chapter must not kill a 2000-chapter run.
                    message = str(exc) or type(exc).__name__
                    result.failed.append((chapter, message))
                    log.warning("Chapter %d (%s) failed: %s", chapter.id, chapter.title, message)
                    self.progress.advance(chapter, False)

        self.progress.start(len(pending), "Downloading")
        try:
            async with asyncio.TaskGroup() as group:
                for _ in range(min(self.concurrency, len(pending))):
                    group.create_task(worker())
        finally:
            self.progress.finish()
        if result.aborted:
            log.error("Stopped early: %s", result.aborted)
        return result

    async def _download_one(self, chapter: Chapter) -> None:
        raw = self.crawler.prefetched.pop(chapter.url, None)
        if raw is None:
            raw = await self.crawler.get_chapter_content(chapter)
        body = self.cleaner.clean(raw, base_url=chapter.url, title=chapter.title)
        if not has_text(body):
            raise EmptyChapterError("no readable text on the page (locked or removed chapter?)")
        if self.images is not None:
            body = await self.images.localize(body)
        self.store.save_chapter(chapter, body)
