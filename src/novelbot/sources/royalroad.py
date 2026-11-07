"""Royal Road (royalroad.com): original web fiction.

Notes
-----
* The fiction page embeds the full chapter list as ``window.chapters`` JSON
  (with volume ids); the HTML ``#chapters`` table is used as a fallback.
* Chapter pages contain hidden anti-piracy paragraphs whose random class
  names are declared ``display: none`` in a ``<style>`` block. They are
  removed so they don't show up in your book.
* Royal Road sits behind Cloudflare. If it challenges your connection,
  novelbot stops with a clear error instead of trying to bypass it.
"""

from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup, Tag

from novelbot.errors import ParseError
from novelbot.models import Chapter, NovelInfo
from novelbot.sources.base import BaseCrawler

_FICTION = re.compile(r"/fiction/(\d+)(?:/([^/?#]+))?", re.I)
_HIDDEN_CLASS = re.compile(r"\.([A-Za-z][\w-]*)\s*\{[^}]*display\s*:\s*none", re.I)
_STATUSES = {"ONGOING", "COMPLETED", "HIATUS", "STUB", "DROPPED", "INACTIVE"}


def _js_array(html: str, name: str) -> list[dict] | None:
    """Extract ``window.<name> = [...];`` from a script as Python data."""
    match = re.search(rf"window\.{name}\s*=\s*(\[.*?\])\s*;\s*\n", html, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return None
    return data if isinstance(data, list) else None


class RoyalRoadCrawler(BaseCrawler):
    name = "royalroad"
    display_name = "Royal Road"
    base_urls = ("https://www.royalroad.com/",)
    min_delay = 1.0
    remove_selectors = (".portlet-footer", ".nav-buttons", ".bold.uppercase.text-center")
    author_note_selectors = (".author-note-portlet", ".author-note")
    bad_text_patterns = (
        r"\b(stolen|unauthori[sz]ed|pirated|misappropriated|unlawfully|taken without)\b"
        r".{0,120}\broyal\s*road\b",
        r"\broyal\s*road\b.{0,120}\b(stolen|unauthori[sz]ed|pirated|report (it|the violation))\b",
    )

    def normalize_url(self, url: str) -> str:
        match = _FICTION.search(url)
        if not match:
            raise ParseError("Not a Royal Road fiction or chapter link", url=url)
        fiction_id, slug = match.groups()
        if slug in (None, "chapter"):
            slug = ""
        return f"https://www.royalroad.com/fiction/{fiction_id}/{slug}".rstrip("/")

    async def get_info(self, url: str) -> NovelInfo:
        soup = await self.get_soup(url, cache=True)
        title = (self.text(soup, ".fic-title h1") or self.text(soup, "h1.font-white")
                 or self.meta(soup, "og:title") or "")
        authors = (self.texts(soup, ".fic-title h4 a") or self.texts(soup, ".fic-title a[href*='/profile/']")
                   or [a for a in [self.meta(soup, "books:author")] if a])
        cover = soup.select_one(".cover-art-container img, .fic-header img.thumbnail, img.thumbnail")
        cover_url = (cover.get("src") if cover else None) or self.meta(soup, "og:image")
        synopsis = soup.select_one(".description .hidden-content") or soup.select_one(".description")
        tags = self.texts(soup, ".tags a.fiction-tag") or self.texts(soup, "span.tags a")
        labels = {t.upper() for t in self.texts(soup, ".fiction-info span.label, .label")}
        status = next((s.lower() for s in _STATUSES if s in labels), None)
        return NovelInfo(
            title=title,
            url=url,
            authors=authors,
            cover_url=self.absolute_url(str(cover_url)) if cover_url else None,
            synopsis=str(synopsis) if synopsis else "",
            tags=tags,
            status=status,
        )

    async def get_chapters(self, url: str) -> list[Chapter]:
        html = await self.get_html(url, cache=True)
        data = _js_array(html, "chapters")
        if data:
            volumes = {v.get("id"): v.get("title") for v in (_js_array(html, "volumes") or [])}
            data = sorted(data, key=lambda c: c.get("order", 0))
            return [
                Chapter(
                    title=str(c.get("title") or "").strip(),
                    url=self.absolute_url(str(c["url"])),
                    volume=volumes.get(c.get("volumeId")) or None,
                )
                for c in data
                if c.get("url") and c.get("visible", 1)
            ]
        soup = BeautifulSoup(html, "lxml")
        rows = soup.select("#chapters tbody tr")
        chapters = []
        for row in rows:
            link = row.select_one("td a[href]")
            href = row.get("data-url") or (link.get("href") if link else None)
            if href:
                chapters.append(Chapter(title=link.get_text(" ", strip=True) if link else "",
                                        url=self.absolute_url(str(href))))
        return chapters

    async def get_chapter_content(self, chapter: Chapter) -> Tag:
        soup = await self.get_soup(chapter.url)
        content = self.select_one_or_fail(soup, ".chapter-inner.chapter-content, .chapter-content",
                                          chapter.url)
        hidden: set[str] = set()
        for style in soup.find_all("style"):
            hidden.update(_HIDDEN_CLASS.findall(style.get_text()))
        for cls in hidden:
            for el in content.select(f".{cls}"):
                el.decompose()
        return content
