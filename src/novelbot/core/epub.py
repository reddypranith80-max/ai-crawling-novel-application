"""EPUB 3 builder (with an EPUB 2 ``toc.ncx`` for older readers).

Produces one self-contained book for any number of chapters. Chapters are
streamed from the cache into the zip one at a time, so memory use stays flat
even for novels with thousands of chapters.

Layout inside the archive::

    mimetype                      (first, uncompressed)
    META-INF/container.xml
    OEBPS/content.opf
    OEBPS/nav.xhtml               EPUB 3 contents (also shown in the book)
    OEBPS/toc.ncx                 EPUB 2 contents
    OEBPS/styles/style.css
    OEBPS/images/...              cover + chapter images
    OEBPS/text/cover.xhtml, info.xhtml, s001.xhtml (volume pages), c00001.xhtml ...
"""

from __future__ import annotations

import logging
import os
import re
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from html import escape
from pathlib import Path

from lxml import etree
from lxml import html as lxml_html

from novelbot.config import EpubSettings
from novelbot.core.cleaner import ContentCleaner, html_to_text, strip_invalid_xml
from novelbot.core.images import MEDIA_TYPES
from novelbot.core.storage import NovelStore
from novelbot.core.toc import TocSection, build_sections, chapter_heading
from novelbot.models import Chapter, Novel, utc_now

log = logging.getLogger(__name__)

_XML_NAME = re.compile(r"^[A-Za-z_][\w.\-]*$")

DEFAULT_CSS = """\
body { margin: 0 4%; font-family: serif; line-height: 1.5; }
h1, h2, h3 { text-align: center; line-height: 1.25; page-break-after: avoid; }
h1 { font-size: 1.6em; margin: 1.5em 0 0.5em; }
h2 { font-size: 1.3em; margin: 1.2em 0 1em; }
p { margin: 0 0 0.8em; text-indent: 0; }
img { max-width: 100%; height: auto; }
blockquote { margin: 1em 1.5em; font-style: italic; }
table { border-collapse: collapse; margin: 1em auto; max-width: 100%; }
td, th { border: 1px solid #888; padding: 0.25em 0.5em; }
hr { border: none; border-top: 1px solid #999; margin: 1.5em 20%; }
.cover { margin: 0; padding: 0; text-align: center; }
.cover img { max-height: 100%; }
.info { text-align: center; }
.info .authors { font-size: 1.1em; font-style: italic; }
.info .meta { font-size: 0.85em; color: #555; }
.synopsis { text-align: left; margin-top: 2em; }
.volume { text-align: center; margin-top: 35%; }
nav ol { list-style-type: none; padding-left: 1em; }
"""


@dataclass(slots=True)
class _Item:
    id: str
    href: str
    media_type: str
    properties: str | None = None


def fragment_to_xhtml(html: str, *, on_image: Callable[[str], str | None] | None = None) -> str:
    """Convert a cleaned HTML fragment into well-formed XHTML markup.

    ``on_image`` maps each ``src`` to its path inside the book, or returns
    None to drop the image. If the markup cannot be made well-formed, the
    text is kept as plain paragraphs so no chapter is ever lost.
    """
    html = strip_invalid_xml(html or "")
    if not html.strip():
        return ""
    try:
        container = lxml_html.fragment_fromstring(html, create_parent="div")
        for el in list(container.iter()):
            if el is container:
                continue
            if not isinstance(el.tag, str):  # comments, processing instructions
                el.drop_tree()
                continue
            if not _XML_NAME.match(el.tag):
                el.drop_tag()
                continue
            for name in list(el.attrib):
                if not _XML_NAME.match(name):
                    del el.attrib[name]
            if el.tag == "img":
                new_src = on_image(el.get("src", "")) if on_image else el.get("src")
                if not new_src:
                    el.drop_tree()
                    continue
                el.set("src", new_src)
                el.set("alt", el.get("alt", ""))
        parts = [escape(container.text or "", quote=False)]
        parts += [etree.tostring(child, encoding="unicode", method="xml") for child in container]
        markup = "".join(parts)
        etree.fromstring(f"<div>{markup}</div>")  # well-formedness check
        return markup
    except (etree.ParserError, etree.XMLSyntaxError, ValueError) as exc:
        log.debug("Falling back to plain text for a fragment: %s", exc)
        paragraphs = [p for p in html_to_text(html).split("\n\n") if p.strip()]
        return "".join(f"<p>{escape(p)}</p>" for p in paragraphs)


def _page(title: str, body: str, lang: str, *, css: str = "../styles/style.css",
          body_class: str | None = None) -> str:
    cls = f' class="{body_class}"' if body_class else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"'
        f' lang="{escape(lang)}" xml:lang="{escape(lang)}">\n'
        f"<head>\n<meta charset=\"utf-8\"/>\n<title>{escape(title)}</title>\n"
        f'<link rel="stylesheet" type="text/css" href="{css}"/>\n</head>\n'
        f"<body{cls}>\n{body}\n</body>\n</html>\n"
    )


class EpubBuilder:
    def __init__(
        self,
        novel: Novel,
        chapters: list[Chapter],
        store: NovelStore,
        settings: EpubSettings | None = None,
    ) -> None:
        self.novel = novel
        self.chapters = chapters
        self.store = store
        self.settings = settings or EpubSettings()
        self.lang = novel.info.language or "en"
        self.uid = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, novel.url or novel.title))
        self._manifest: list[_Item] = []
        self._spine: list[str] = []
        self._images: dict[str, str] = {}  # file name in images_dir -> href in book

    # ------------------------------------------------------------------ build
    def build(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        try:
            with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                            compress_type=zipfile.ZIP_STORED)
                zf.writestr("META-INF/container.xml", self._container_xml())
                self._write_css(zf)
                cover_href = self._write_cover(zf)
                if self.settings.include_info_page:
                    self._add_page(zf, "info", "text/info.xhtml", self._info_page())
                sections = build_sections(self.chapters, self.settings.toc_group_size)
                self._add_item("nav", "nav.xhtml", "application/xhtml+xml", "nav")
                self._spine.append("nav")
                section_pages = self._write_sections(zf, sections)
                self._write_images(zf)
                zf.writestr("OEBPS/nav.xhtml", self._nav(sections, section_pages, cover_href))
                self._add_item("ncx", "toc.ncx", "application/x-dtbncx+xml")
                zf.writestr("OEBPS/toc.ncx", self._ncx(sections, section_pages))
                zf.writestr("OEBPS/content.opf", self._opf(cover_href is not None))
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return path

    # -------------------------------------------------------------- pieces
    def _add_item(self, item_id: str, href: str, media_type: str,
                  properties: str | None = None) -> None:
        self._manifest.append(_Item(item_id, href, media_type, properties))

    def _add_page(self, zf: zipfile.ZipFile, item_id: str, href: str, content: str) -> None:
        zf.writestr(f"OEBPS/{href}", content)
        self._add_item(item_id, href, "application/xhtml+xml")
        self._spine.append(item_id)

    @staticmethod
    def _container_xml() -> str:
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles>\n</container>\n'
        )

    def _write_css(self, zf: zipfile.ZipFile) -> None:
        css = DEFAULT_CSS
        if self.settings.stylesheet:
            try:
                css = Path(self.settings.stylesheet).read_text("utf-8")
            except OSError as exc:
                log.warning("Cannot read stylesheet %s (%s); using default",
                            self.settings.stylesheet, exc)
        zf.writestr("OEBPS/styles/style.css", css)
        self._add_item("css", "styles/style.css", "text/css")

    def _write_cover(self, zf: zipfile.ZipFile) -> str | None:
        cover = self.store.cover_path() if self.settings.include_cover else None
        if cover is None:
            return None
        ext = cover.suffix.lower().lstrip(".").replace("jpeg", "jpg")
        media_type = MEDIA_TYPES.get(ext)
        if media_type is None:
            return None
        href = f"images/cover.{ext}"
        zf.write(cover, f"OEBPS/{href}")
        self._add_item("cover-image", href, media_type, "cover-image")
        body = (f'<div class="cover"><img src="../{href}" '
                f'alt="{escape(self.novel.title)}"/></div>')
        self._add_page(zf, "cover", "text/cover.xhtml",
                       _page(self.novel.title, body, self.lang, body_class="cover"))
        return "text/cover.xhtml"

    def _info_page(self) -> str:
        info = self.novel.info
        lines = [f"<h1>{escape(info.title)}</h1>"]
        if info.authors:
            lines.append(f'<p class="authors">by {escape(", ".join(info.authors))}</p>')
        meta = []
        if info.status:
            meta.append(f"Status: {escape(info.status)}")
        first, last = self.chapters[0].id, self.chapters[-1].id
        total = len(self.novel.chapters)
        meta.append(f"Chapters {first}–{last} of {total}" if len(self.chapters) != total
                    else f"{total} chapters")
        if info.url:
            meta.append(f'Source: <a href="{escape(info.url)}">{escape(self.novel.source)}</a>')
        meta.append(f"Downloaded {escape(utc_now()[:10])} with novelbot")
        lines.append('<p class="meta">' + "<br/>".join(meta) + "</p>")
        if info.tags:
            lines.append(f'<p class="meta">{escape(", ".join(info.tags))}</p>')
        if info.synopsis:
            synopsis = fragment_to_xhtml(ContentCleaner(keep_images=False).clean(info.synopsis))
            if synopsis:
                lines.append(f'<div class="synopsis">{synopsis}</div>')
        return _page(info.title, '<section class="info">\n' + "\n".join(lines) + "\n</section>",
                     self.lang)

    def _write_sections(self, zf: zipfile.ZipFile, sections: list[TocSection]) -> dict[int, str]:
        """Write volume pages + chapters. Returns section index -> volume page href."""
        section_pages: dict[int, str] = {}
        for index, section in enumerate(sections, start=1):
            if section.kind == "volume" and section.title:
                href = f"text/s{index:03d}.xhtml"
                body = f'<section epub:type="part"><h1 class="volume">{escape(section.title)}</h1></section>'
                self._add_page(zf, f"s{index:03d}", href, _page(section.title, body, self.lang))
                section_pages[index] = href
            for chapter in section.chapters:
                self._write_chapter(zf, chapter)
        return section_pages

    def _write_chapter(self, zf: zipfile.ZipFile, chapter: Chapter) -> None:
        body_html = self.store.load_body(chapter) or ""
        title = chapter_heading(chapter)
        content = fragment_to_xhtml(body_html, on_image=self._image_href)
        body = (f'<section epub:type="chapter" role="doc-chapter">\n<h2>{escape(title)}</h2>\n'
                f"{content}\n</section>")
        self._add_page(zf, f"c{chapter.id:05d}", f"text/c{chapter.id:05d}.xhtml",
                       _page(title, body, self.lang))

    def _image_href(self, src: str) -> str | None:
        if not src.startswith("images/"):
            return None  # remote images are not allowed inside an EPUB
        name = src.removeprefix("images/")
        if "/" in name or not (self.store.images_dir / name).is_file():
            return None
        self._images[name] = f"images/{name}"
        return f"../images/{name}"

    def _write_images(self, zf: zipfile.ZipFile) -> None:
        for n, (name, href) in enumerate(sorted(self._images.items()), start=1):
            ext = name.rsplit(".", 1)[-1].lower()
            media_type = MEDIA_TYPES.get(ext)
            if media_type is None:
                continue
            zf.write(self.store.images_dir / name, f"OEBPS/{href}")
            self._add_item(f"img{n:05d}", href, media_type)

    # ---------------------------------------------------------- navigation
    def _nav(self, sections: list[TocSection], section_pages: dict[int, str],
             cover_href: str | None) -> str:
        out = ['<nav epub:type="toc" id="toc" role="doc-toc">', "<h1>Contents</h1>", "<ol>"]
        if self.settings.include_info_page:
            out.append(f'<li><a href="text/info.xhtml">{escape(self.novel.title)}</a></li>')
        for index, section in enumerate(sections, start=1):
            items = "".join(
                f'<li><a href="text/c{ch.id:05d}.xhtml">{escape(chapter_heading(ch))}</a></li>'
                for ch in section.chapters
            )
            if section.kind == "flat":
                out.append(items)
                continue
            target = section_pages.get(index, f"text/c{section.chapters[0].id:05d}.xhtml")
            out.append(f'<li><a href="{target}">{escape(section.title or "")}</a>'
                       f"<ol>{items}</ol></li>")
        out.append("</ol>\n</nav>")
        first_chapter = f"text/c{self.chapters[0].id:05d}.xhtml"
        landmarks = ['<nav epub:type="landmarks" id="landmarks" hidden="hidden">', "<ol>"]
        if cover_href:
            landmarks.append(f'<li><a epub:type="cover" href="{cover_href}">Cover</a></li>')
        landmarks.append('<li><a epub:type="toc" href="nav.xhtml#toc">Contents</a></li>')
        landmarks.append(f'<li><a epub:type="bodymatter" href="{first_chapter}">Start</a></li>')
        landmarks.append("</ol>\n</nav>")
        return _page("Contents", "\n".join(out + landmarks), self.lang, css="styles/style.css")

    def _ncx(self, sections: list[TocSection], section_pages: dict[int, str]) -> str:
        play_order: dict[str, int] = {}
        counter = 0

        def point(label: str, src: str, children: str = "") -> str:
            nonlocal counter
            if src not in play_order:
                play_order[src] = len(play_order) + 1
            counter += 1
            return (f'<navPoint id="np{counter}" playOrder="{play_order[src]}">'
                    f"<navLabel><text>{escape(label)}</text></navLabel>"
                    f'<content src="{src}"/>{children}</navPoint>')

        points: list[str] = []
        if self.settings.include_info_page:
            points.append(point(self.novel.title, "text/info.xhtml"))
        depth = 1
        for index, section in enumerate(sections, start=1):
            if section.kind == "flat":
                points += [point(chapter_heading(ch), f"text/c{ch.id:05d}.xhtml")
                           for ch in section.chapters]
                continue
            depth = 2
            target = section_pages.get(index, f"text/c{section.chapters[0].id:05d}.xhtml")
            # Reserve the section's play order before its children.
            if target not in play_order:
                play_order[target] = len(play_order) + 1
            children = "".join(point(chapter_heading(ch), f"text/c{ch.id:05d}.xhtml")
                               for ch in section.chapters)
            points.append(point(section.title or "", target, children))
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            f'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" '
            f'xml:lang="{escape(self.lang)}">\n<head>'
            f'<meta name="dtb:uid" content="{escape(self.uid)}"/>'
            f'<meta name="dtb:depth" content="{depth}"/>'
            '<meta name="dtb:totalPageCount" content="0"/>'
            '<meta name="dtb:maxPageNumber" content="0"/></head>\n'
            f"<docTitle><text>{escape(self.novel.title)}</text></docTitle>\n"
            f"<navMap>\n{''.join(points)}\n</navMap>\n</ncx>\n"
        )

    def _opf(self, has_cover: bool) -> str:
        info = self.novel.info
        meta = [
            f'<dc:identifier id="book-id">{escape(self.uid)}</dc:identifier>',
            f"<dc:title>{escape(info.title)}</dc:title>",
            f"<dc:language>{escape(self.lang)}</dc:language>",
        ]
        meta += [f'<dc:creator id="creator{i}">{escape(a)}</dc:creator>'
                 for i, a in enumerate(info.authors, start=1)]
        if info.url:
            meta.append(f"<dc:source>{escape(info.url)}</dc:source>")
        meta.append(f"<dc:publisher>{escape(self.novel.source)}</dc:publisher>")
        if info.synopsis:
            meta.append(f"<dc:description>{escape(html_to_text(info.synopsis))}</dc:description>")
        meta += [f"<dc:subject>{escape(tag)}</dc:subject>" for tag in info.tags]
        meta.append(f'<meta property="dcterms:modified">{utc_now()}</meta>')
        if has_cover:
            meta.append('<meta name="cover" content="cover-image"/>')
        manifest = "\n".join(
            f'<item id="{it.id}" href="{escape(it.href)}" media-type="{it.media_type}"'
            + (f' properties="{it.properties}"' if it.properties else "") + "/>"
            for it in self._manifest
        )
        spine = "\n".join(f'<itemref idref="{idref}"/>' for idref in self._spine)
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
            f'unique-identifier="book-id" xml:lang="{escape(self.lang)}">\n'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            + "\n".join(meta)
            + f"\n</metadata>\n<manifest>\n{manifest}\n</manifest>\n"
            f'<spine toc="ncx">\n{spine}\n</spine>\n</package>\n'
        )
