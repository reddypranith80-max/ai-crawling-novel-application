"""Turn raw chapter HTML into a small, safe, portable fragment.

The cleaner keeps only a whitelist of formatting tags (paragraphs, emphasis,
headings, lists, tables, images ...), drops scripts/ads/forms, removes every
attribute except a handful (``href``, ``src``, ``alt``, ``colspan`` ...) and
resolves lazy-loaded images. The result is stored in the chapter cache and
is what the EPUB, PDF and TXT exporters consume.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

#: Removed together with their contents.
DROP_TAGS = {
    "script", "style", "noscript", "iframe", "frame", "frameset", "object", "embed",
    "form", "input", "button", "select", "textarea", "option", "label", "nav", "aside",
    "ins", "svg", "canvas", "audio", "video", "source", "track", "map", "link", "meta",
    "head", "title", "template", "dialog",
}
#: Kept as elements.
KEEP_TAGS = {
    "p", "br", "hr", "div", "h1", "h2", "h3", "h4", "h5", "h6",
    "b", "strong", "i", "em", "u", "s", "strike", "del", "sub", "sup", "small",
    "blockquote", "pre", "code", "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption",
    "a", "img", "figure", "figcaption",
}
KEEP_ATTRS = {
    "a": {"href"},
    "img": {"src", "alt"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
    "ol": {"start"},
}
BLOCK_TAGS = {
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "ul", "ol", "li",
    "dl", "dt", "dd", "table", "thead", "tbody", "tfoot", "tr", "caption", "figure",
    "figcaption", "hr", "section", "article", "header", "footer", "main",
}
LAZY_SRC_ATTRS = ("data-src", "data-lazy-src", "data-original", "data-url", "src")
#: Common advertising / share-widget class or id fragments removed everywhere.
AD_SELECTORS = (
    "[class*='adsbygoogle']", "[id*='google_ads']", "[class*='ad-container']",
    "[class*='advert']", "[class*='sharedaddy']", "[class*='social-share']",
    "[class*='code-block']", "[class*='ezoic']",
)
# Characters that are illegal in XML 1.0 (they break EPUB/XHTML).
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_WS = re.compile(r"\s+")


def strip_invalid_xml(text: str) -> str:
    return _INVALID_XML.sub("", text)


def normalize_space(text: str) -> str:
    return _WS.sub(" ", text.replace("\xa0", " ")).strip()


class ContentCleaner:
    """Configurable HTML sanitiser for chapter bodies and synopses."""

    def __init__(
        self,
        *,
        remove_selectors: Iterable[str] = (),
        bad_text_patterns: Iterable[str] = (),
        keep_images: bool = True,
    ) -> None:
        self.remove_selectors = (*AD_SELECTORS, *remove_selectors)
        self.bad_text = [re.compile(p, re.IGNORECASE) for p in bad_text_patterns]
        self.keep_images = keep_images

    def clean(self, content: str | Tag, *, base_url: str = "", title: str | None = None) -> str:
        """Return a cleaned HTML fragment (may be an empty string)."""
        html = content if isinstance(content, str) else str(content)
        soup = BeautifulSoup(strip_invalid_xml(html), "lxml")
        root: Tag = soup.body or soup

        for comment in root.find_all(string=lambda s: isinstance(s, Comment)):
            comment.extract()
        for tag in root.find_all(DROP_TAGS):
            tag.decompose()
        for selector in self.remove_selectors:
            for tag in root.select(selector):
                tag.decompose()
        if self.bad_text:
            self._drop_bad_text(root)

        for tag in list(root.find_all(True)):
            if tag.decomposed:
                continue
            self._sanitize_tag(tag, base_url)

        self._drop_empty(root)
        self._collapse_breaks(root)
        if title:
            self._drop_duplicate_title(root, title)
        return "".join(str(child) for child in root.contents).strip()

    # ---------------------------------------------------------------- helpers
    def _sanitize_tag(self, tag: Tag, base_url: str) -> None:
        name = tag.name
        if name == "img":
            src = next((tag.get(a) for a in LAZY_SRC_ATTRS if tag.get(a)), None)
            if not self.keep_images or not src or str(src).startswith("data:image/svg"):
                tag.decompose()
                return
            src = str(src).strip()
            tag.attrs = {"src": src if src.startswith("data:") else urljoin(base_url, src),
                         "alt": str(tag.get("alt") or "")}
            return
        if name not in KEEP_TAGS:
            if name in ("section", "article", "header", "footer", "main", "center"):
                tag.name = "div"
            else:
                tag.unwrap()  # span, font and unknown/custom tags keep only their text
                return
        allowed = KEEP_ATTRS.get(tag.name, set())
        tag.attrs = {k: v for k, v in tag.attrs.items() if k in allowed}
        if tag.name == "a":
            href = str(tag.get("href") or "").strip()
            if not href or href.startswith(("javascript:", "#", "mailto:")):
                tag.unwrap()
            else:
                tag["href"] = urljoin(base_url, href)

    def _drop_bad_text(self, root: Tag) -> None:
        for tag in root.find_all(["p", "div", "span", "h1", "h2", "h3", "h4", "h5", "h6"]):
            if tag.decomposed or tag.find(BLOCK_TAGS):
                continue
            text = tag.get_text(" ", strip=True)
            if text and any(p.search(text) for p in self.bad_text):
                tag.decompose()

    @staticmethod
    def _drop_empty(root: Tag) -> None:
        removable = {"p", "div", "span", "h1", "h2", "h3", "h4", "h5", "h6", "li", "b",
                     "strong", "i", "em", "u", "blockquote", "a", "figure", "small"}
        changed = True
        while changed:
            changed = False
            for tag in root.find_all(removable):
                if tag.decomposed:
                    continue
                if tag.find(["img", "hr", "table"]):
                    continue
                if normalize_space(tag.get_text()) == "" and not tag.find("br"):
                    tag.decompose()
                    changed = True

    @staticmethod
    def _collapse_breaks(root: Tag) -> None:
        """Turn runs of 3+ <br> into two, and strip leading/trailing <br>s in blocks."""
        for br in root.find_all("br"):
            if br.decomposed:
                continue
            run = [br]
            sibling = br.next_sibling
            while sibling is not None:
                if isinstance(sibling, NavigableString) and not sibling.strip():
                    sibling = sibling.next_sibling
                    continue
                if isinstance(sibling, Tag) and sibling.name == "br":
                    run.append(sibling)
                    sibling = sibling.next_sibling
                    continue
                break
            for extra in run[2:]:
                extra.decompose()

    @staticmethod
    def _drop_duplicate_title(root: Tag, title: str) -> None:
        """Remove the first text block if it just repeats the chapter title.

        Exporters print the title themselves, so a body that starts with
        "Chapter 1: Foo" would otherwise show it twice.
        """
        wanted = _alnum(title)
        if not wanted:
            return
        containers = BLOCK_TAGS - {"hr"}
        for tag in root.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "div", "strong", "b"]):
            if tag.decomposed or tag.find(containers):
                continue  # a wrapper; look inside it instead
            text = _alnum(tag.get_text())
            if not text:
                continue
            if text == wanted and not tag.find("img"):
                tag.decompose()
            return


def _alnum(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower())


def html_to_text(html: str) -> str:
    """Convert a cleaned HTML fragment into readable plain text paragraphs."""
    soup = BeautifulSoup(html, "lxml")
    root = soup.body or soup
    for node in list(root.find_all(string=True)):
        if isinstance(node, Comment):
            node.extract()
        elif node.parent is not None and node.parent.name != "pre":
            node.replace_with(_WS.sub(" ", str(node).replace("\xa0", " ")))
    for br in root.find_all("br"):
        br.replace_with("\n")
    for img in root.find_all("img"):
        alt = img.get("alt")
        img.replace_with(f"[image: {alt}]" if alt else "[image]")
    for li in root.find_all("li"):
        li.insert_before("\n• ")  # one item per line, no blank line between items
    for tag in root.find_all(BLOCK_TAGS - {"li"}):
        tag.insert_before("\n\n")
        tag.insert_after("\n\n")
    text = root.get_text()
    lines = [line.strip() for line in text.split("\n")]
    out: list[str] = []
    blank = 0
    for line in lines:
        if not line:
            blank += 1
            continue
        if out and blank:
            out.append("")
        out.append(line)
        blank = 0
    return "\n".join(out)


def has_text(html: str) -> bool:
    """True when a fragment contains readable text or at least one image."""
    if "<img" in html:
        return True
    return bool(normalize_space(BeautifulSoup(html, "lxml").get_text()))
