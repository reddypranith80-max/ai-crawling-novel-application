"""PDF builder (fpdf2): one PDF with a clickable contents section and bookmarks.

* A Unicode TrueType font is auto-detected (Georgia / DejaVu Serif /
  Liberation Serif ...), or configured via ``[pdf] font = "..."``.
* The contents pages are generated after all chapters are laid out, so
  every entry has a correct page number and link, however many chapters
  there are (1000+ chapters simply produce more contents pages).
* Volumes or 100-chapter blocks become top-level bookmarks with the
  chapters nested below them, so PDF viewers show a navigable outline.
* Chapters whose HTML is too complex for fpdf2's renderer fall back to
  plain paragraphs instead of failing the whole book.
"""

from __future__ import annotations

import logging
import math
import platform
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup
from fpdf import FPDF
from fpdf.errors import FPDFException
from fpdf.fonts import TextStyle
from fpdf.outline import TableOfContents

from novelbot.config import PdfSettings
from novelbot.core.cleaner import ContentCleaner, html_to_text
from novelbot.core.storage import NovelStore
from novelbot.core.toc import build_sections, chapter_heading
from novelbot.models import Chapter, Novel, utc_now

log = logging.getLogger(__name__)

FONT = "body"

_WINDOWS = Path("C:/Windows/Fonts")
_FONT_CANDIDATES: list[tuple[Path, Path, Path, Path]] = [
    (_WINDOWS / "georgia.ttf", _WINDOWS / "georgiab.ttf", _WINDOWS / "georgiai.ttf",
     _WINDOWS / "georgiaz.ttf"),
    (_WINDOWS / "cambria.ttc", _WINDOWS / "cambriab.ttf", _WINDOWS / "cambriai.ttf",
     _WINDOWS / "cambriaz.ttf"),
    (_WINDOWS / "arial.ttf", _WINDOWS / "arialbd.ttf", _WINDOWS / "ariali.ttf",
     _WINDOWS / "arialbi.ttf"),
    *[
        (Path(d) / "DejaVuSerif.ttf", Path(d) / "DejaVuSerif-Bold.ttf",
         Path(d) / "DejaVuSerif-Italic.ttf", Path(d) / "DejaVuSerif-BoldItalic.ttf")
        for d in ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/TTF",
                  "/usr/share/fonts/dejavu", "/usr/share/fonts/dejavu-serif-fonts")
    ],
    *[
        (Path(d) / "LiberationSerif-Regular.ttf", Path(d) / "LiberationSerif-Bold.ttf",
         Path(d) / "LiberationSerif-Italic.ttf", Path(d) / "LiberationSerif-BoldItalic.ttf")
        for d in ("/usr/share/fonts/truetype/liberation", "/usr/share/fonts/liberation-serif",
                  "/usr/share/fonts/TTF")
    ],
    (Path("/System/Library/Fonts/Supplemental/Georgia.ttf"),
     Path("/System/Library/Fonts/Supplemental/Georgia Bold.ttf"),
     Path("/System/Library/Fonts/Supplemental/Georgia Italic.ttf"),
     Path("/System/Library/Fonts/Supplemental/Georgia Bold Italic.ttf")),
]
#: Used, in order, for characters the main font lacks: extra Latin/Greek/Cyrillic,
#: symbols, then Chinese / Japanese / Korean (common in translated web novels).
_FALLBACK_CANDIDATES = [
    _WINDOWS / "segoeui.ttf", _WINDOWS / "seguisym.ttf",
    _WINDOWS / "msyh.ttc", _WINDOWS / "YuGothM.ttc", _WINDOWS / "msgothic.ttc",
    _WINDOWS / "malgun.ttf",
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc"),
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
    Path("/System/Library/Fonts/AppleSDGothicNeo.ttc"),
]
BLOCKS_INSIDE_DIV = ["p", "div", "ul", "ol", "table", "blockquote", "pre", "h1", "h2", "h3",
                     "h4", "h5", "h6", "hr", "figure", "dl"]
_LATIN1_REPLACEMENTS = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-",
    "\u2014": "--", "\u2026": "...", "\u2022": "*", "\u00a0": " ", "\u200b": "",
})


@dataclass(slots=True)
class _Fonts:
    unicode: bool
    fallbacks: list[str]


class _Book(FPDF):
    def __init__(self, *args: object, footer_font: str, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._footer_font = footer_font
        self.show_footer = False

    def footer(self) -> None:
        if not self.show_footer:
            return
        self.set_y(-12)
        self.set_font(self._footer_font, size=8)
        self.set_text_color(120)
        self.cell(0, 6, str(self.page_no()), align="C")
        self.set_text_color(0)


class PdfBuilder:
    def __init__(
        self,
        novel: Novel,
        chapters: list[Chapter],
        store: NovelStore,
        settings: PdfSettings | None = None,
        *,
        toc_group_size: int = 100,
        on_chapter: Callable[[Chapter], None] | None = None,
    ) -> None:
        self.novel = novel
        self.chapters = chapters
        self.store = store
        self.settings = settings or PdfSettings()
        self.toc_group_size = toc_group_size
        self.on_chapter = on_chapter
        self._fonts = _Fonts(unicode=False, fallbacks=[])

    # ---------------------------------------------------------------- fonts
    def _setup_fonts(self, pdf: FPDF) -> str:
        s = self.settings
        if s.font:
            regular = Path(s.font)
            styles = {"": regular, "B": s.font_bold or regular, "I": s.font_italic or regular,
                      "BI": s.font_bold_italic or s.font_bold or regular}
        else:
            found = next((c for c in _FONT_CANDIDATES if c[0].is_file()), None)
            if found is None:
                log.warning("No Unicode TrueType font found; PDF will use a basic Latin font. "
                            "Set [pdf] font = \"/path/to/font.ttf\" for full Unicode support.")
                self._fonts = _Fonts(unicode=False, fallbacks=[])
                return "times"
            regular, bold, italic, bold_italic = found
            styles = {"": regular, "B": bold, "I": italic, "BI": bold_italic}
        for style, path in styles.items():
            path = Path(path)
            pdf.add_font(FONT, style, path if path.is_file() else styles[""])
        fallbacks: list[str] = []
        extra = [Path(p) for p in s.fallback_fonts] + [p for p in _FALLBACK_CANDIDATES
                                                      if p.is_file()]
        for n, path in enumerate(dict.fromkeys(extra)):
            if not path.is_file():
                log.warning("Fallback font %s not found", path)
                continue
            name = f"fallback{n}"
            try:
                pdf.add_font(name, "", path)
                fallbacks.append(name)
            except (FPDFException, OSError, RuntimeError) as exc:
                log.debug("Cannot use fallback font %s: %s", path, exc)
        if fallbacks:
            pdf.set_fallback_fonts(fallbacks, exact_match=False)
        self._fonts = _Fonts(unicode=True, fallbacks=fallbacks)
        return FONT

    def _text(self, value: str) -> str:
        """Make text safe for the active font (only matters for core fonts)."""
        if self._fonts.unicode:
            return value
        return value.translate(_LATIN1_REPLACEMENTS).encode("latin-1", "replace").decode("latin-1")

    # ---------------------------------------------------------------- build
    def build(self, path: Path) -> Path:
        # Glyph/subsetting notices are very noisy for big books and harmless.
        for name in ("fpdf", "fontTools"):
            logging.getLogger(name).setLevel(logging.ERROR)
        path.parent.mkdir(parents=True, exist_ok=True)
        size = self.settings.page_size.upper() if self.settings.page_size.upper() in (
            "A3", "A4", "A5", "A6", "LETTER", "LEGAL") else "A5"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)  # "A5 dimensions fixed in 2.8.5"
            pdf = _Book(format=size.lower() if size in ("LETTER", "LEGAL") else size, unit="mm",
                        footer_font="helvetica")
        family = self.family = self._setup_fonts(pdf)
        pdf._footer_font = family
        pdf.set_title(self._text(self.novel.title))
        pdf.set_author(self._text(", ".join(self.novel.info.authors)))
        pdf.set_creator("novelbot")
        pdf.set_subject(self._text(f"Downloaded from {self.novel.url}"))
        pdf.set_margins(16, 16, 16)
        pdf.set_auto_page_break(auto=True, margin=18)
        pdf.set_font(family, size=self.settings.font_size)

        self._title_page(pdf)
        self._toc(pdf)
        pdf.show_footer = True
        sections = build_sections(self.chapters, self.toc_group_size)
        for section in sections:
            level = 0
            if section.kind != "flat":
                pdf.add_page()
                pdf.start_section(self._text(section.title or ""), level=0)
                if section.kind == "volume":
                    pdf.set_y(pdf.h / 3)
                    pdf.set_font(family, "B", self.settings.font_size + 8)
                    pdf.multi_cell(0, 10, self._text(section.title or ""), align="C")
                    pdf.set_font(family, size=self.settings.font_size)
                    pdf.add_page()
                level = 1
            for index, chapter in enumerate(section.chapters):
                # The first chapter of a volume/group already has a fresh page.
                if not (index == 0 and section.kind in ("volume", "group")):
                    pdf.add_page()
                self._chapter(pdf, chapter, level)
                if self.on_chapter:
                    self.on_chapter(chapter)

        tmp = path.with_name(f".{path.name}.tmp")
        try:
            pdf.output(str(tmp))
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
        return path

    # -------------------------------------------------------------- pages
    def _title_page(self, pdf: FPDF) -> None:
        info, family, size = self.novel.info, self.family, self.settings.font_size
        pdf.add_page()
        cover = self.store.cover_path()
        if cover is not None and self.settings.include_images:
            try:
                width = pdf.epw * 0.75
                pdf.image(str(cover), x=(pdf.w - width) / 2, y=pdf.t_margin, w=width,
                          keep_aspect_ratio=True, h=pdf.eph * 0.6)
                pdf.set_y(pdf.t_margin + pdf.eph * 0.6 + 6)
            except Exception as exc:  # broken/unsupported image must not kill the book
                log.debug("Cover not embedded in PDF: %s", exc)
                pdf.set_y(pdf.h / 4)
        else:
            pdf.set_y(pdf.h / 4)
        pdf.set_font(family, "B", size + 9)
        pdf.multi_cell(0, 10, self._text(info.title), align="C")
        if info.authors:
            pdf.ln(2)
            pdf.set_font(family, "I", size + 2)
            pdf.multi_cell(0, 7, self._text("by " + ", ".join(info.authors)), align="C")
        pdf.ln(4)
        pdf.set_font(family, size=size - 2)
        first, last, total = self.chapters[0].id, self.chapters[-1].id, len(self.novel.chapters)
        lines = [f"Chapters {first}-{last} of {total}" if len(self.chapters) != total
                 else f"{total} chapters"]
        if info.status:
            lines.append(f"Status: {info.status}")
        lines.append(f"Source: {info.url}")
        lines.append(f"Downloaded {utc_now()[:10]} with novelbot")
        pdf.set_text_color(90)
        pdf.multi_cell(0, 5, self._text("\n".join(lines)), align="C")
        pdf.set_text_color(0)
        if info.synopsis:
            pdf.add_page()
            pdf.set_font(family, "B", size + 3)
            pdf.cell(0, 10, "Synopsis", align="C", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font(family, size=size)
            synopsis = ContentCleaner(keep_images=False).clean(info.synopsis)
            self._write_body(pdf, synopsis)

    def _toc(self, pdf: FPDF) -> None:
        pdf.add_page()
        pdf.set_font(self.family, "B", self.settings.font_size + 4)
        pdf.cell(0, 12, "Contents", align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font(self.family, size=self.settings.font_size - 1)
        toc = TableOfContents(
            text_style=TextStyle(font_family=self.family, font_size_pt=self.settings.font_size - 1),
            level_indent=6,
            line_spacing=1.4,
        )
        pdf.insert_toc_placeholder(toc.render_toc, pages=1, allow_extra_pages=True)

    def _chapter(self, pdf: FPDF, chapter: Chapter, level: int) -> None:
        title = self._text(chapter_heading(chapter))
        pdf.start_section(title, level=level)
        pdf.set_font(self.family, "B", self.settings.font_size + 3)
        pdf.multi_cell(0, 8, title, align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
        pdf.set_font(self.family, size=self.settings.font_size)
        self._write_body(pdf, self.store.load_body(chapter) or "")

    def _write_body(self, pdf: FPDF, html: str) -> None:
        html = self._prepare_html(pdf, html)
        if not html.strip():
            return
        state = (pdf.page, pdf.y)
        try:
            pdf.write_html(html, font_family=self.family, warn_on_tags_not_matching=False,
                           image_map=self._image_path)
            return
        except (FPDFException, ValueError, KeyError, IndexError, NotImplementedError,
                AttributeError, TypeError, OSError) as exc:
            log.debug("HTML rendering failed (%s); using plain text", exc)
        # Plain-text fallback. Content already written stays; continue below it.
        if (pdf.page, pdf.y) != state:
            pdf.ln(4)
        pdf.set_font(self.family, size=self.settings.font_size)
        for paragraph in html_to_text(html).split("\n\n"):
            pdf.multi_cell(0, self.settings.font_size * 0.5, self._text(paragraph),
                           new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)

    def _image_path(self, src: str) -> str:
        return str(self.store.root / src)

    def _prepare_html(self, pdf: FPDF, html: str) -> str:
        """Size images to fit the page, drop unsupported bits, fix text for core fonts."""
        soup = BeautifulSoup(html, "lxml")
        root = soup.body or soup
        for img in root.find_all("img"):
            src = str(img.get("src", ""))
            local = self.store.root / src
            if (not self.settings.include_images or not src.startswith("images/")
                    or not local.is_file() or img.find_parent("table")):
                alt = str(img.get("alt") or "").strip()
                img.replace_with(f"[{alt}]" if alt else "")
                continue
            width_mm = self._fit_image(pdf, local)
            if width_mm is None:
                img.replace_with("")
                continue
            img.attrs = {"src": src, "width": f"{width_mm * pdf.k:.0f}"}
            img.insert_before(soup.new_tag("br"))
            img.insert_after(soup.new_tag("br"))
        for tag in root.find_all(["figure", "figcaption", "dl", "dt", "dd"]):
            tag.name = "div" if tag.name != "dt" else "b"
        # fpdf2 only applies line spacing to <p>; plain-text <div>s (text separated
        # by <br>) become paragraphs so they get the same comfortable spacing.
        for div in root.find_all("div"):
            if not div.find(BLOCKS_INSIDE_DIV):
                div.name = "p"
        for p in root.find_all("p"):
            p.attrs["line-height"] = "1.35"
        markup = "".join(str(c) for c in root.contents)
        return markup if self._fonts.unicode else self._text(markup)

    @staticmethod
    def _fit_image(pdf: FPDF, path: Path) -> float | None:
        try:
            from PIL import Image

            with Image.open(path) as im:
                w_px, h_px = im.size
        except Exception:
            return None
        if not w_px or not h_px:
            return None
        natural_mm = w_px * 25.4 / 96
        max_w, max_h = pdf.epw, pdf.eph * 0.85
        width = min(natural_mm, max_w)
        if width * h_px / w_px > max_h:
            width = max_h * w_px / h_px
        return max(5.0, math.floor(width))


def describe_font() -> str:
    """Human-readable name of the auto-detected PDF font (for `config show`)."""
    found = next((c[0] for c in _FONT_CANDIDATES if c[0].is_file()), None)
    return str(found) if found else f"none found on {platform.system()} (core Latin font)"


__all__ = ["PdfBuilder", "describe_font"]
