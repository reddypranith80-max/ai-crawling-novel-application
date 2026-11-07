"""The BaseCrawler interface that every source implements.

Adding a site takes one file in ``novelbot/sources/`` (or in a plugin dir)::

    class MySiteCrawler(BaseCrawler):
        name = "mysite"
        display_name = "My Site"
        base_urls = ("https://www.mysite.com/",)

        async def get_info(self, url):            # required
            soup = await self.get_soup(url, cache=True)
            return NovelInfo(title=self.text(soup, "h1"), url=url)

        async def get_chapters(self, url):        # required
            soup = await self.get_soup(url, cache=True)
            return [Chapter(title=a.get_text(strip=True), url=self.absolute_url(a["href"]))
                    for a in soup.select(".chapter-list a")]

        async def get_chapter_content(self, chapter):  # required
            soup = await self.get_soup(chapter.url)
            return self.select_one_or_fail(soup, ".chapter-content", chapter.url)

Optional hooks: ``normalize_url`` (map chapter URLs to the novel page) and
``initialize`` (set cookies or headers before the first request). The
subclass is registered automatically as soon as its module is imported.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Any, ClassVar
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from novelbot.config import Settings
from novelbot.core.http import HttpClient, host_of
from novelbot.errors import ParseError
from novelbot.models import Chapter, NovelInfo

log = logging.getLogger(__name__)

_REGISTRY: dict[str, type[BaseCrawler]] = {}


def registered_crawlers() -> dict[str, type[BaseCrawler]]:
    return dict(_REGISTRY)


class BaseCrawler(ABC):
    #: Unique identifier, used by ``--source`` and in ``meta.json``.
    name: ClassVar[str] = ""
    display_name: ClassVar[str] = ""
    #: URL prefixes this crawler handles. ``www.`` is optional when matching.
    base_urls: ClassVar[tuple[str, ...]] = ()
    language: ClassVar[str] = "en"
    #: Minimum seconds between requests for this site (on top of config).
    min_delay: ClassVar[float] = 0.0
    #: CSS selectors removed from every chapter body.
    remove_selectors: ClassVar[tuple[str, ...]] = ()
    #: Regexes; paragraphs whose text matches are removed (watermarks etc.).
    bad_text_patterns: ClassVar[tuple[str, ...]] = ()
    #: Selectors of author's notes; removed unless ``keep_author_notes``.
    author_note_selectors: ClassVar[tuple[str, ...]] = ()
    #: Set False for helper base classes that should not be registered.
    register: ClassVar[bool] = True

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("register", True) and cls.name:
            if cls.name in _REGISTRY and _REGISTRY[cls.name] is not cls:
                log.debug("Crawler %r re-registered by %s", cls.name, cls.__qualname__)
            _REGISTRY[cls.name] = cls

    def __init__(self, http: HttpClient, settings: Settings) -> None:
        self.http = http
        self.settings = settings
        self._page_cache: OrderedDict[str, str] = OrderedDict()
        #: Chapter bodies fetched ahead of time (url -> html), e.g. when a site
        #: serves the whole book on one page. Checked by the downloader first.
        self.prefetched: dict[str, str] = {}
        #: Upper bound of chapters the user asked for (None = all). Crawlers
        #: that must walk "next" links can stop early once they reach it.
        self.discovery_limit: int | None = None
        for base in self.base_urls:
            if self.min_delay:
                self.http.limiter.set_min_delay(host_of(base), self.min_delay)

    # ------------------------------------------------------------- matching
    @classmethod
    def hosts(cls) -> set[str]:
        hosts = set()
        for base in cls.base_urls:
            host = host_of(base)
            hosts.add(host)
            hosts.add(host.removeprefix("www."))
        return hosts

    @classmethod
    def can_handle(cls, url: str) -> bool:
        host = host_of(url)
        return host in cls.hosts() or host.removeprefix("www.") in cls.hosts()

    # ---------------------------------------------------------------- hooks
    async def initialize(self) -> None:
        """Called once before any other method. Override to set cookies etc."""

    def normalize_url(self, url: str) -> str:
        """Map any URL of the novel (e.g. a chapter link) to its main page."""
        return url

    @abstractmethod
    async def get_info(self, url: str) -> NovelInfo:
        """Return title, authors, cover, synopsis and tags of the novel."""

    @abstractmethod
    async def get_chapters(self, url: str) -> list[Chapter]:
        """Return every chapter in reading order (oldest first)."""

    @abstractmethod
    async def get_chapter_content(self, chapter: Chapter) -> str | Tag:
        """Return the chapter body as HTML (string or BeautifulSoup tag)."""

    # -------------------------------------------------------------- helpers
    async def get_html(self, url: str, *, cache: bool = False, **kwargs: Any) -> str:
        """GET a page. ``cache=True`` keeps the last few pages in memory so
        ``get_info`` and ``get_chapters`` can share one request."""
        if cache and url in self._page_cache:
            self._page_cache.move_to_end(url)
            return self._page_cache[url]
        html = await self.http.get_text(url, **kwargs)
        if cache:
            self._page_cache[url] = html
            while len(self._page_cache) > 8:
                self._page_cache.popitem(last=False)
        return html

    async def get_soup(self, url: str, *, cache: bool = False, **kwargs: Any) -> BeautifulSoup:
        return BeautifulSoup(await self.get_html(url, cache=cache, **kwargs), "lxml")

    async def post_soup(self, url: str, data: dict[str, Any], **kwargs: Any) -> BeautifulSoup:
        response = await self.http.request("POST", url, data=data, **kwargs)
        return BeautifulSoup(response.text, "lxml")

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        return json.loads(await self.get_html(url, **kwargs))

    def absolute_url(self, href: str, base: str | None = None) -> str:
        return urljoin(base or (self.base_urls[0] if self.base_urls else ""), href.strip())

    @staticmethod
    def text(soup: BeautifulSoup | Tag, selector: str, default: str = "") -> str:
        el = soup.select_one(selector)
        return el.get_text(" ", strip=True) if el else default

    @staticmethod
    def texts(soup: BeautifulSoup | Tag, selector: str) -> list[str]:
        seen: list[str] = []
        for el in soup.select(selector):
            value = el.get_text(" ", strip=True)
            if value and value not in seen:
                seen.append(value)
        return seen

    @staticmethod
    def meta(soup: BeautifulSoup, prop: str) -> str | None:
        el = soup.select_one(f"meta[property='{prop}'], meta[name='{prop}']")
        value = el.get("content") if el else None
        return str(value).strip() if value else None

    @staticmethod
    def select_one_or_fail(soup: BeautifulSoup | Tag, selector: str, url: str) -> Tag:
        el = soup.select_one(selector)
        if el is None:
            raise ParseError(f"Could not find {selector!r} on the page", url=url)
        return el

    @staticmethod
    def same_site(url: str, other: str) -> bool:
        a = (urlsplit(url).hostname or "").removeprefix("www.")
        b = (urlsplit(other).hostname or "").removeprefix("www.")
        return a == b
