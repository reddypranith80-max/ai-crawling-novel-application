"""Archive of Our Own (archiveofourown.org): fan fiction.

Notes
-----
* The whole work is fetched once with ``view_full_work=true`` and split
  into chapters, so a 200-chapter work costs one request instead of 200.
  The chapter bodies are handed to the downloader through ``prefetched``.
* ``view_adult=true`` skips the content-warning interstitial.
* Works restricted to logged-in users are reported, not worked around.
* AO3 refuses some automated clients outright (HTTP 418). novelbot reports
  that as "blocked" and stops; it never tries to disguise itself.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from novelbot.errors import AccessDeniedError, ParseError
from novelbot.models import Chapter, NovelInfo
from novelbot.sources.base import BaseCrawler

_WORK = re.compile(r"/works/(\d+)", re.I)
_BASE = "https://archiveofourown.org"


class AO3Crawler(BaseCrawler):
    name = "ao3"
    display_name = "Archive of Our Own"
    base_urls = ("https://archiveofourown.org/", "https://ao3.org/")
    min_delay = 3.0
    remove_selectors = ("h3.landmark", ".landmark.heading", "#feedback", ".kudos")
    author_note_selectors = (".notes", ".end.notes", ".preface .notes")

    def normalize_url(self, url: str) -> str:
        match = _WORK.search(url)
        if not match:
            raise ParseError("Not an AO3 work link (expected /works/<id>)", url=url)
        return f"{_BASE}/works/{match.group(1)}"

    def _full_work_url(self, url: str) -> str:
        return f"{url}?view_adult=true&view_full_work=true"

    async def _work_page(self, url: str) -> BeautifulSoup:
        soup = await self.get_soup(self._full_work_url(url), cache=True)
        page_text = soup.get_text(" ", strip=True)
        if "only available to registered users" in page_text or (soup.select_one(
            "#signin form, form#new_user_session_small"
        ) and not soup.select_one("#workskin")):
            raise AccessDeniedError(
                "This AO3 work is only available to logged-in users and cannot be downloaded."
            )
        return soup

    async def get_info(self, url: str) -> NovelInfo:
        soup = await self._work_page(url)
        title = self.text(soup, "#workskin h2.title") or self.text(soup, "h2.title.heading")
        authors = self.texts(soup, "#workskin h3.byline a[rel='author']") or [
            self.text(soup, "#workskin h3.byline") or "Anonymous"
        ]
        summary = soup.select_one("#workskin .preface .summary blockquote.userstuff")
        tags = []
        for cls in ("fandom", "relationship", "character", "freeform"):
            tags += self.texts(soup, f"dd.{cls}.tags a.tag")
        language = soup.select_one("dd.language")
        status = None
        chapters_stat = self.text(soup, "dl.stats dd.chapters")
        if "/" in chapters_stat:
            done, _, planned = chapters_stat.partition("/")
            status = "completed" if done.strip() == planned.strip() else "ongoing"
        return NovelInfo(
            title=title,
            url=url,
            authors=authors,
            synopsis=str(summary) if summary else "",
            tags=tags,
            language=str(language.get("lang") or "en") if language else "en",
            status=status,
        )

    async def get_chapters(self, url: str) -> list[Chapter]:
        soup = await self._work_page(url)
        chapters: list[Chapter] = []
        blocks = soup.select("#chapters > div.chapter[id^='chapter-']")
        for number, block in enumerate(blocks, start=1):
            heading = block.select_one("h3.title")
            link = heading.select_one("a[href]") if heading else None
            href = str(link["href"]) if link else f"/works/{_WORK.search(url).group(1)}#chapter-{number}"
            title = heading.get_text() if heading else f"Chapter {number}"
            chapter = Chapter(title=re.sub(r"\s+", " ", title).strip(),
                              url=self.absolute_url(href, _BASE))
            body = block.select_one("div.userstuff[role='article'], div.userstuff")
            if body is not None:
                self.prefetched[chapter.url] = str(self._with_notes(block, body))
            chapters.append(chapter)
        if not chapters:
            # Single-chapter work: the text sits directly under #chapters.
            body = soup.select_one("#chapters div.userstuff")
            if body is None:
                raise ParseError("Could not find the work text", url=url)
            title = self.text(soup, "#workskin h2.title") or "Chapter 1"
            chapter = Chapter(title=title, url=url)
            self.prefetched[url] = str(body)
            chapters.append(chapter)
        return chapters

    def _with_notes(self, block: Tag, body: Tag) -> Tag:
        """Keep chapter notes next to the text (removed later unless configured)."""
        wrapper = BeautifulSoup("<div></div>", "lxml").div
        for notes in block.select(".chapter.preface .notes, .preface.group .notes"):
            wrapper.append(notes)
        wrapper.append(body)
        for notes in block.select(".end.notes"):
            wrapper.append(notes)
        return wrapper

    async def get_chapter_content(self, chapter: Chapter) -> Tag:
        # Normally served from `prefetched`; this path is used when resuming.
        soup = await self.get_soup(f"{chapter.url.split('#')[0]}?view_adult=true")
        return self.select_one_or_fail(
            soup, "#chapters div.userstuff[role='article'], #chapters div.userstuff", chapter.url
        )
