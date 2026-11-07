"""Scribble Hub (scribblehub.com): original web fiction.

Notes
-----
* The table of contents is paginated (15 chapters per page, newest first)
  via ``?toc=N``. The page count comes from the embedded pagination script
  (``items: N``) or from the chapter count. Chapters are sorted by their
  ``order`` attribute, so the result is always oldest-first.
* The AJAX endpoint the site's own JavaScript uses is behind a Cloudflare
  challenge for non-browser clients, so plain GET pages are used instead.
* Scribble Hub's Cloudflare setup reacts to request bursts; a 2 second
  minimum delay is applied for this site.
"""

from __future__ import annotations

import logging
import math
import re

from bs4 import BeautifulSoup, Tag

from novelbot.errors import ParseError
from novelbot.models import Chapter, NovelInfo
from novelbot.sources.base import BaseCrawler

log = logging.getLogger(__name__)

_SERIES = re.compile(r"/series/(\d+)(?:/([^/?#]+))?", re.I)
_READ = re.compile(r"/read/(\d+)-([^/?#]+)", re.I)
_PAGES = re.compile(r"#pagination-mesh-toc'\)\.pagination\(\{\s*items:\s*(\d+)", re.S)
MAX_TOC_PAGES = 1000


class ScribbleHubCrawler(BaseCrawler):
    name = "scribblehub"
    display_name = "Scribble Hub"
    base_urls = ("https://www.scribblehub.com/",)
    min_delay = 2.0
    remove_selectors = (".wi_news", ".modern-footnotes-footnote__connector", ".sp-wrap")
    author_note_selectors = (".wi_authornotes",)

    def normalize_url(self, url: str) -> str:
        if (match := _SERIES.search(url)) or (match := _READ.search(url)):
            series_id, slug = match.groups()
        else:
            raise ParseError("Not a Scribble Hub series or chapter link", url=url)
        path = f"/series/{series_id}/{slug}/" if slug else f"/series/{series_id}/"
        return "https://www.scribblehub.com" + path

    async def get_info(self, url: str) -> NovelInfo:
        soup = await self.get_soup(url, cache=True)
        title = self.text(soup, ".fic_title") or self.meta(soup, "og:title") or ""
        cover = soup.select_one(".fic_image img")
        cover_url = (cover.get("src") if cover else None) or self.meta(soup, "og:image")
        synopsis = soup.select_one(".wi_fic_desc")
        tags = self.texts(soup, ".wi_fic_genre .fic_genre") + self.texts(soup, ".wi_fic_showtags .stag")
        return NovelInfo(
            title=title,
            url=url,
            authors=self.texts(soup, ".auth_name_fic"),
            cover_url=str(cover_url) if cover_url else None,
            synopsis=str(synopsis) if synopsis else "",
            tags=tags,
            status=self._status(soup),
        )

    @staticmethod
    def _status(soup: BeautifulSoup) -> str | None:
        icon = soup.select_one("i.status")
        holder = icon.find_parent("li") if icon else None
        text = holder.get_text(" ", strip=True) if holder else ""
        match = re.search(r"\b(Ongoing|Completed|Hiatus|Dropped)\b", text, re.I)
        return match.group(1).lower() if match else None

    async def get_chapters(self, url: str) -> list[Chapter]:
        html = await self.get_html(url, cache=True)
        soup = BeautifulSoup(html, "lxml")
        first_page = self._parse_toc(soup)
        if not first_page:
            raise ParseError("No chapters in the table of contents", url=url)
        pages = self._page_count(html, soup, len(first_page))
        found: dict[str, tuple[float, Chapter]] = {}
        for order, ch in first_page:
            found[ch.url] = (order, ch)
        for page in range(2, min(pages, MAX_TOC_PAGES) + 1):
            if page % 10 == 0:
                log.info("Scribble Hub table of contents: page %d/%d", page, pages)
            entries = self._parse_toc(await self.get_soup(f"{url}?toc={page}"))
            if not entries:
                break
            for order, ch in entries:
                found.setdefault(ch.url, (order, ch))
        return [ch for _, ch in sorted(found.values(), key=lambda item: item[0])]

    def _parse_toc(self, soup: BeautifulSoup) -> list[tuple[float, Chapter]]:
        entries = []
        for li in soup.select("ol.toc_ol li.toc_w"):
            link = li.select_one("a.toc_a[href]") or li.select_one("a[href]")
            if link is None:
                continue
            try:
                order = float(li.get("order") or 0)
            except ValueError:
                order = 0.0
            entries.append((order, Chapter(title=link.get_text(" ", strip=True),
                                           url=self.absolute_url(str(link["href"])))))
        return entries

    @staticmethod
    def _page_count(html: str, soup: BeautifulSoup, per_page: int) -> int:
        if match := _PAGES.search(html):
            return int(match.group(1))
        count = soup.select_one(".wi_novel_title.toc .cnt_toc")
        if count and count.get_text(strip=True).isdigit() and per_page:
            return math.ceil(int(count.get_text(strip=True)) / per_page)
        return 1

    async def get_chapter_content(self, chapter: Chapter) -> Tag:
        soup = await self.get_soup(chapter.url)
        return self.select_one_or_fail(soup, "#chp_raw, .chp_raw", chapter.url)
