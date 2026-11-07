"""The engine: URL -> crawler -> metadata -> chapters -> books.

Programmatic use::

    import asyncio
    from novelbot.config import load_settings
    from novelbot.core.engine import Engine
    from novelbot.core.selection import Selection

    async def main():
        settings = load_settings()
        async with Engine(settings) as engine:
            session = await engine.open("https://www.scribblehub.com/series/123/x/")
            chapters = Selection(first=10).apply(session.novel)
            await engine.download(session, chapters)
            print(engine.export(session.novel, session.store, chapters, ["epub", "pdf"]))

    asyncio.run(main())
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from novelbot.config import Settings
from novelbot.core.cleaner import ContentCleaner
from novelbot.core.downloader import ChapterDownloader, DownloadResult, ProgressHooks
from novelbot.core.export import export_novel
from novelbot.core.http import HttpClient
from novelbot.core.images import ImageCache, download_cover
from novelbot.core.selection import Selection
from novelbot.core.storage import META_FILE, NovelStore
from novelbot.errors import NovelBotError, ParseError
from novelbot.models import Chapter, Novel
from novelbot.sources import create_crawler
from novelbot.sources.base import BaseCrawler

log = logging.getLogger(__name__)

StatusCallback = Callable[[str], None]


@dataclass(slots=True)
class NovelSession:
    novel: Novel
    crawler: BaseCrawler
    store: NovelStore


class Engine:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.http = HttpClient(
            settings.http,
            transport=transport,
            max_connections=max(4, settings.download.concurrency * 2),
        )

    async def __aenter__(self) -> Engine:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.http.aclose()

    # ----------------------------------------------------------------- open
    async def open(
        self,
        url: str,
        *,
        source: str | None = None,
        selection: Selection | None = None,
        on_status: StatusCallback | None = None,
        persist: bool = True,
    ) -> NovelSession:
        """Resolve the crawler, fetch metadata + chapter list, prepare the folder.

        With ``persist=False`` nothing is written to disk (used by ``info``).
        """
        status = on_status or (lambda _msg: None)
        url = url.strip()
        if not url.lower().startswith(("http://", "https://")):
            raise NovelBotError(f"Not a web address: {url!r} (it must start with https://)")

        crawler = create_crawler(url, self.http, self.settings, source=source)
        if selection is not None:
            crawler.discovery_limit = selection.upper_bound()
        await crawler.initialize()
        url = crawler.normalize_url(url)

        status(f"Reading novel info from {crawler.display_name or crawler.name}")
        info = await crawler.get_info(url)
        info.url = info.url or url
        if not info.title:
            raise ParseError("Could not find the novel title", url=url)

        status("Fetching chapter list")
        chapters = _dedupe(await crawler.get_chapters(url))
        if not chapters:
            raise ParseError("No chapters found", url=url)

        novel = Novel(info=info, source=crawler.name, chapters=chapters)
        novel.number_chapters()
        store = NovelStore.for_novel(self.settings.download.output_dir, novel)
        if not persist:
            return NovelSession(novel, crawler, store)
        store.save_meta(novel)

        if info.cover_url and store.cover_path() is None:
            status("Downloading cover")
            await download_cover(self.http, info.cover_url, store.root)
        return NovelSession(novel, crawler, store)

    # ------------------------------------------------------------- download
    async def download(
        self,
        session: NovelSession,
        chapters: list[Chapter],
        *,
        force: bool = False,
        progress: ProgressHooks | None = None,
    ) -> DownloadResult:
        crawler, opts = session.crawler, self.settings.download
        removals = list(crawler.remove_selectors)
        if not opts.keep_author_notes:
            removals += crawler.author_note_selectors
        cleaner = ContentCleaner(
            remove_selectors=removals,
            bad_text_patterns=crawler.bad_text_patterns,
            keep_images=opts.download_images,
        )
        images = ImageCache(self.http, session.store.images_dir) if opts.download_images else None
        downloader = ChapterDownloader(
            crawler, session.store, cleaner,
            concurrency=opts.concurrency, images=images, progress=progress,
        )
        return await downloader.run(chapters, force=force)

    # --------------------------------------------------------------- export
    def export(
        self,
        novel: Novel,
        store: NovelStore,
        chapters: list[Chapter],
        formats: list[str] | None = None,
        *,
        on_status: StatusCallback | None = None,
        on_chapter: Callable[[Chapter], None] | None = None,
    ) -> dict[str, Path]:
        available = store.downloaded(chapters)
        missing = len(chapters) - len(available)
        if missing:
            log.warning("%d selected chapter(s) are not downloaded and will be left out", missing)
        return export_novel(
            novel, available, store, formats or self.settings.download.formats, self.settings,
            on_status=on_status, on_chapter=on_chapter,
        )


def load_local(path: Path) -> tuple[Novel, NovelStore]:
    """Open an already-downloaded novel folder (or its meta.json) without network."""
    root = path.parent if path.name == META_FILE else path
    store = NovelStore(root)
    novel = store.load_meta()
    if novel is None:
        raise NovelBotError(f"No downloaded novel found in {root} (missing {META_FILE})")
    return novel, store


def _dedupe(chapters: list[Chapter]) -> list[Chapter]:
    seen: set[str] = set()
    unique = []
    for ch in chapters:
        if ch.url and ch.url not in seen:
            seen.add(ch.url)
            unique.append(ch)
    if len(unique) != len(chapters):
        log.debug("Dropped %d duplicate chapter link(s)", len(chapters) - len(unique))
    return unique
