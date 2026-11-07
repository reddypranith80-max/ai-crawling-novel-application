"""Generic crawler for sites without a dedicated source.

It works in three ways, picked automatically:

1. **Configured selectors** -- a ``[sites.<name>]`` table in the config gives
   CSS selectors for title, chapter list, content, etc. Most reliable.
2. **Table-of-contents heuristics** -- given the novel's index page, it finds
   the block of links that look like chapters ("Chapter 12", ``/chapter-12``).
3. **Follow mode** -- given the URL of a *chapter* (e.g. chapter 1), it reads
   that page and follows "Next chapter" links until the end. This handles
   sites that have no chapter index at all.

Content is located with a list of common selectors, then by scoring which
element holds the most paragraph text.
"""

from __future__ import annotations

import logging
import re
from typing import ClassVar

from bs4 import BeautifulSoup, Tag

from novelbot.config import Settings, SiteConfig
from novelbot.core.cleaner import normalize_space
from novelbot.core.http import HttpClient
from novelbot.errors import ParseError
from novelbot.models import Chapter, NovelInfo
from novelbot.sources.base import BaseCrawler

log = logging.getLogger(__name__)

MAX_FOLLOW = 10000
MAX_TOC_PAGES = 500

_CHAPTER_TEXT = re.compile(
    r"\b(chapter|chap|ch|episode|ep|part|prologue|epilogue|interlude|side\s*story|"
    r"extra|afterword|vol(ume)?)\b\.?\s*[\divxlc]*",
    re.I,
)
_CHAPTER_HREF = re.compile(r"(chapter|chap|episode|/ch[-_]?\d|/c\d+(/|$)|[-_/]ep[-_]?\d)", re.I)
_NEXT_TEXT = re.compile(
    r"^(next(\s+(chapter|chap|page|part|episode))?|continue\s+reading|siguiente)"
    r"\s*[>»→›]*$|^[>»→›]+$",
    re.I,
)
_NUMBER = re.compile(r"(\d+(?:\.\d+)?)")
CONTENT_SELECTORS = (
    "#chapter-content", ".chapter-content", "#chr-content", ".chr-c", "#chaptercontent",
    ".chapter__content", ".chapter-body", ".reading-content .text-left", ".reading-content",
    ".text-left", ".entry-content", ".post-content", ".novel-content", "#novel-content",
    "#content .text", "article .content", "#content", ".content", "article", "main",
)


class GenericCrawler(BaseCrawler):
    name = "generic"
    display_name = "Generic"
    base_urls: ClassVar[tuple[str, ...]] = ()

    def __init__(self, http: HttpClient, settings: Settings,
                 site: SiteConfig | None = None) -> None:
        super().__init__(http, settings)
        self.site = site or SiteConfig(name="")
        if site is not None:
            self.display_name = f"{site.name} (config)"
            self.remove_selectors = tuple(site.remove)
            self.bad_text_patterns = tuple(site.bad_text)
            if site.delay:
                for host in site.hosts:
                    http.limiter.set_min_delay(host.lower(), site.delay)
        else:
            self.display_name = "Generic (heuristics)"

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return False  # only used as an explicit fallback

    # ----------------------------------------------------------------- info
    async def get_info(self, url: str) -> NovelInfo:
        soup = await self.get_soup(url, cache=True)
        s = self.site
        title = self._select_text(soup, s.title)
        if not title:
            og_title, h1, page_title = (self.meta(soup, "og:title"), self.text(soup, "h1"),
                                        self.text(soup, "title"))
            if self._is_chapter_page(soup, url):
                # "Big Story - Chapter 1" -> "Big Story". The <h1> of a chapter page is
                # usually just the chapter heading, so it is the last resort.
                stripped = [_strip_chapter_part(t) for t in (og_title, page_title, h1) if t]
                title = next((t for t in stripped if t), h1 or page_title or "")
            else:
                title = og_title or h1 or page_title or ""
        authors = self._select_texts(soup, s.author) or [
            a for a in [self.meta(soup, "author"), self.meta(soup, "book:author"),
                        self.text(soup, "[itemprop='author']"), self.text(soup, "a[rel='author']")]
            if a
        ][:1]
        return NovelInfo(
            title=normalize_space(title),
            url=url,
            authors=authors,
            cover_url=self._cover(soup, url),
            synopsis=self._synopsis(soup),
            tags=self._select_texts(soup, s.tags),
            language=s.language or "en",
        )

    def _cover(self, soup: BeautifulSoup, url: str) -> str | None:
        el = soup.select_one(self.site.cover) if self.site.cover else None
        if el is not None:
            value = el.get("content") if el.name == "meta" else (
                el.get("data-src") or el.get("data-lazy-src") or el.get("src"))
            if value:
                return self.absolute_url(str(value), url)
        value = self.meta(soup, "og:image")
        return self.absolute_url(value, url) if value else None

    def _synopsis(self, soup: BeautifulSoup) -> str:
        if self.site.synopsis and (el := soup.select_one(self.site.synopsis)):
            return str(el)
        return self.meta(soup, "og:description") or self.meta(soup, "description") or ""

    # ------------------------------------------------------------- chapters
    async def get_chapters(self, url: str) -> list[Chapter]:
        soup = await self.get_soup(url, cache=True)
        s = self.site
        if s.chapter_list:
            chapters = await self._chapters_from_selector(url, soup)
        elif s.next_chapter:
            return await self._follow(url, soup)
        else:
            chapters = self._guess_chapter_links(soup, url)
            if len(chapters) < 2:
                if self._find_content(soup) is None:
                    raise ParseError("Could not find chapter links or chapter text. Add a "
                                     "[sites] entry with selectors for this site", url=url)
                log.info("No chapter index found; following 'next chapter' links from this page")
                return await self._follow(url, soup)
        if s.reverse_chapters or (not s.chapter_list and _looks_descending(chapters)):
            chapters.reverse()
        return chapters

    async def _chapters_from_selector(self, url: str, soup: BeautifulSoup) -> list[Chapter]:
        chapters: list[Chapter] = []
        seen_pages = {url}
        page_url = url
        for _ in range(MAX_TOC_PAGES):
            for a in soup.select(self.site.chapter_list or ""):
                href = a.get("href")
                if href:
                    chapters.append(Chapter(title=normalize_space(a.get_text(" ")),
                                            url=self.absolute_url(str(href), page_url)))
            nxt = soup.select_one(self.site.toc_next) if self.site.toc_next else None
            href = nxt.get("href") if nxt is not None else None
            if not href:
                break
            page_url = self.absolute_url(str(href), page_url)
            if page_url in seen_pages:
                break
            seen_pages.add(page_url)
            soup = await self.get_soup(page_url)
        return chapters

    def _guess_chapter_links(self, soup: BeautifulSoup, url: str) -> list[Chapter]:
        candidates: list[tuple[Tag, Chapter]] = []
        seen: set[str] = set()
        for a in soup.select("a[href]"):
            href = str(a["href"]).strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            absolute = self.absolute_url(href, url).split("#")[0]
            text = normalize_space(a.get_text(" "))
            if (absolute in seen or absolute.rstrip("/") == url.rstrip("/")
                    or not self.same_site(absolute, url) or not text or len(text) > 200):
                continue
            if _CHAPTER_TEXT.search(text) or _CHAPTER_HREF.search(absolute):
                seen.add(absolute)
                candidates.append((a, Chapter(title=text, url=absolute)))
        if not candidates:
            return []
        # Prefer the container holding most chapter links (skips sidebars/"latest" widgets).
        counts: dict[int, int] = {}
        parents: dict[int, Tag] = {}
        for a, _ in candidates:
            parent = a.find_parent(["ul", "ol", "table", "tbody", "div", "section"])
            if parent is not None:
                counts[id(parent)] = counts.get(id(parent), 0) + 1
                parents[id(parent)] = parent
        if counts:
            best_id = max(counts, key=lambda k: counts[k])
            if counts[best_id] >= max(2, len(candidates) // 3):
                best = parents[best_id]
                return [ch for a, ch in candidates if any(p is best for p in a.parents)]
        return [ch for _, ch in candidates]

    # ---------------------------------------------------------- follow mode
    async def _follow(self, start_url: str, soup: BeautifulSoup | None) -> list[Chapter]:
        chapters: list[Chapter] = []
        seen: set[str] = set()
        limit = self.discovery_limit or MAX_FOLLOW
        url: str | None = start_url
        while url and url not in seen and len(chapters) < limit:
            seen.add(url)
            if soup is None:
                soup = await self.get_soup(url)
            content = self._find_content(soup)
            if content is None:
                if not chapters:
                    raise ParseError("Could not find chapter text on the page", url=url)
                break
            chapter = Chapter(title=self._chapter_title(soup) or f"Chapter {len(chapters) + 1}",
                              url=url)
            chapters.append(chapter)
            self.prefetched[url] = str(content)
            if len(chapters) % 25 == 0:
                log.info("Followed %d chapters so far...", len(chapters))
            url, soup = self._find_next(soup, url), None
        return chapters

    def _find_next(self, soup: BeautifulSoup, current: str) -> str | None:
        el: Tag | None = None
        if self.site.next_chapter:
            el = soup.select_one(self.site.next_chapter)
        else:
            el = soup.select_one("a[rel~='next'], link[rel~='next']")
            if el is None:
                for a in soup.select("a[href]"):
                    text = normalize_space(a.get_text(" "))
                    marker = " ".join([*(a.get("class") or []), str(a.get("id") or "")]).lower()
                    if (text and _NEXT_TEXT.match(text)) or re.search(
                        r"\bnext([_-]?(chap|chapter|page))?\b", marker
                    ):
                        el = a
                        break
        href = el.get("href") if el is not None else None
        if not href:
            return None
        nxt = self.absolute_url(str(href), current).split("#")[0]
        if nxt.rstrip("/") == current.rstrip("/") or not self.same_site(nxt, current):
            return None
        return nxt

    # -------------------------------------------------------------- content
    async def get_chapter_content(self, chapter: Chapter) -> Tag:
        soup = await self.get_soup(chapter.url)
        content = self._find_content(soup)
        if content is None:
            raise ParseError("Could not find chapter text", url=chapter.url)
        if self.site.chapter_title is None:
            for heading in content.select("h1, h2, h3"):
                if normalize_space(heading.get_text()) == chapter.title:
                    heading.decompose()
                    break
        return content

    def _find_content(self, soup: BeautifulSoup) -> Tag | None:
        if self.site.content:
            return soup.select_one(self.site.content)
        for selector in CONTENT_SELECTORS:
            el = soup.select_one(selector)
            if el is not None and _text_score(el) >= 400:
                return el
        best: Tag | None = None
        best_score = 0.0
        for el in soup.find_all(["div", "article", "section", "main", "td"]):
            score = _text_score(el)
            if score > best_score:
                best, best_score = el, score
        return best if best_score >= 200 else None

    def _chapter_title(self, soup: BeautifulSoup) -> str:
        if self.site.chapter_title:
            return self._select_text(soup, self.site.chapter_title)
        for heading in soup.select("h1, h2, h3"):
            text = normalize_space(heading.get_text(" "))
            if text and _CHAPTER_TEXT.search(text) and len(text) < 200:
                return text
        return normalize_space(self.meta(soup, "og:title") or self.text(soup, "title"))

    def _is_chapter_page(self, soup: BeautifulSoup, url: str) -> bool:
        return bool(_CHAPTER_HREF.search(url)) and len(self._guess_chapter_links(soup, url)) < 2

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _select_text(soup: BeautifulSoup, selector: str | None) -> str:
        if not selector:
            return ""
        el = soup.select_one(selector)
        if el is None:
            return ""
        if el.name == "meta":
            return normalize_space(str(el.get("content") or ""))
        return normalize_space(el.get_text(" "))

    def _select_texts(self, soup: BeautifulSoup, selector: str | None) -> list[str]:
        return self.texts(soup, selector) if selector else []


def _text_score(el: Tag) -> float:
    """Amount of prose directly inside ``el`` (its own <p>s and text nodes)."""
    paragraphs = el.find_all("p", recursive=False)
    score = float(sum(len(p.get_text(strip=True)) for p in paragraphs))
    direct = sum(len(s.strip()) for s in el.find_all(string=True, recursive=False))
    if direct and el.find("br", recursive=False):
        score += direct * 0.9  # <br>-separated text without <p> tags
    return score


def _looks_descending(chapters: list[Chapter]) -> bool:
    numbers = []
    for ch in chapters:
        if match := _NUMBER.search(ch.title):
            numbers.append(float(match.group(1)))
    return len(numbers) >= 3 and numbers[0] > numbers[-1]


def _strip_chapter_part(title: str) -> str:
    """'My Novel - Chapter 3: Foo | Site' -> 'My Novel'; '' if only a chapter name."""
    head = re.split(r"\s*[-–|:]*\s*\b(chapter|chap|ch|episode|ep)\b\.?\s*\d", title,
                    maxsplit=1, flags=re.I)[0]
    head = re.split(r"\s+[|–-]\s+", head)[0] if head else head
    return head.strip(" -|:")


__all__ = ["GenericCrawler"]
