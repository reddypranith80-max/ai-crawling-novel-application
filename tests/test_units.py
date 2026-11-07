"""Unit tests: robots.txt, chapter selection, HTML cleaning, TOC grouping, storage."""

from __future__ import annotations

import pytest

from novelbot.core.cleaner import ContentCleaner, html_to_text
from novelbot.core.robots import RobotsTxt
from novelbot.core.selection import Selection, SelectionError, parse_ranges
from novelbot.core.storage import NovelStore, safe_filename
from novelbot.core.toc import build_sections
from novelbot.models import Chapter, Novel, NovelInfo

# --------------------------------------------------------------------------- robots


def test_robots_longest_match_allow_wins() -> None:
    robots = RobotsTxt.parse(
        "User-agent: *\nDisallow: /wp-admin/\nAllow: /wp-admin/admin-ajax.php\n"
    )
    assert robots.can_fetch("novelbot", "https://x.test/wp-admin/admin-ajax.php")
    assert not robots.can_fetch("novelbot", "https://x.test/wp-admin/options.php")
    assert robots.can_fetch("novelbot", "https://x.test/series/1/")


def test_robots_wildcards_and_anchor() -> None:
    robots = RobotsTxt.parse("User-agent: *\nDisallow: /fiction/*/vote\nDisallow: /*.pdf$\n")
    assert not robots.can_fetch("novelbot", "https://x.test/fiction/12/vote")
    assert robots.can_fetch("novelbot", "https://x.test/fiction/12/chapter/3")
    assert not robots.can_fetch("novelbot", "https://x.test/files/a.pdf")
    assert robots.can_fetch("novelbot", "https://x.test/files/a.pdf?x=1")


def test_robots_group_selection_and_crawl_delay() -> None:
    text = (
        "User-agent: *\nDisallow: /account/\nCrawl-delay: 2\n\n"
        "User-agent: Claude-User\nUser-agent: GPTBot\nDisallow: /\n\n"
        "User-agent: novelbot\nDisallow: /private/\n"
    )
    robots = RobotsTxt.parse(text)
    # A specific group replaces '*' entirely.
    assert robots.can_fetch("novelbot", "https://x.test/account/")
    assert not robots.can_fetch("novelbot", "https://x.test/private/a")
    assert not robots.can_fetch("gptbot", "https://x.test/anything")
    assert robots.can_fetch("otherbot", "https://x.test/fiction/")
    assert robots.crawl_delay("otherbot") == 2
    assert robots.crawl_delay("novelbot") is None


def test_robots_empty_disallow_and_robots_txt_always_allowed() -> None:
    robots = RobotsTxt.parse("User-agent: *\nDisallow:\n")
    assert robots.can_fetch("novelbot", "https://x.test/a")
    assert RobotsTxt.parse("User-agent: *\nDisallow: /\n").can_fetch(
        "novelbot", "https://x.test/robots.txt")


# ------------------------------------------------------------------------ selection


def _novel(n: int, volumes: int = 0) -> Novel:
    chapters = [
        Chapter(title=f"C{i}", url=f"https://x.test/{i}",
                volume=(f"Vol {1 + (i - 1) * volumes // n}" if volumes else None))
        for i in range(1, n + 1)
    ]
    novel = Novel(info=NovelInfo(title="T", url="https://x.test/"), source="t", chapters=chapters)
    novel.number_chapters()
    return novel


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("1-3", [1, 2, 3]), ("5,2", [2, 5]), ("8-", [8, 9, 10]), ("-2", [1, 2]),
     ("3..4", [3, 4]), ("9-12", [9, 10]), ("4-2", [2, 3, 4])],
)
def test_parse_ranges(spec: str, expected: list[int]) -> None:
    assert parse_ranges(spec, 10) == expected


@pytest.mark.parametrize("spec", ["abc", "-", "1-2-3"])
def test_parse_ranges_invalid(spec: str) -> None:
    with pytest.raises(SelectionError):
        parse_ranges(spec, 10)


def test_selection_modes() -> None:
    novel = _novel(1500, volumes=3)
    assert len(Selection(all=True).apply(novel)) == 1500
    assert [c.id for c in Selection(first=2).apply(novel)] == [1, 2]
    assert [c.id for c in Selection(last=2).apply(novel)] == [1499, 1500]
    assert len(Selection(volumes="2").apply(novel)) == 500
    assert Selection(ranges="10-20").upper_bound() == 20
    assert Selection(ranges="10-").upper_bound() is None
    with pytest.raises(SelectionError):
        Selection(first=1, last=1).apply(novel)


# -------------------------------------------------------------------------- cleaner


def test_cleaner_removes_junk_and_keeps_text() -> None:
    html = """<div><script>x()</script><p style="a" class="b">Hello <span>world</span></p>
    <div class="adsbygoogle">ad</div><p>   </p><iframe src="x"></iframe>
    <p>Read more at SPAM</p><img data-src="/a.png" src="data:image/gif;base64,AA" alt="A">
    <a href="javascript:void(0)">js</a><a href="/next">link</a><br><br><br><br>end</div>"""
    out = ContentCleaner(bad_text_patterns=[r"read more at"]).clean(html, base_url="https://x.test/c/")
    assert "script" not in out and "iframe" not in out and "adsbygoogle" not in out
    assert "<p>Hello world</p>" in out
    assert "SPAM" not in out
    assert 'src="https://x.test/a.png"' in out
    assert 'href="https://x.test/next"' in out and "javascript" not in out
    assert out.count("<br/>") == 2
    assert "style=" not in out and "class=" not in out


def test_cleaner_drops_duplicate_title_inside_wrapper() -> None:
    html = "<div><div><p><strong>Chapter 1 - The Start</strong></p><p>Body text.</p></div></div>"
    out = ContentCleaner().clean(html, title="Chapter 1: The Start")
    assert "The Start" not in out and "Body text." in out


def test_html_to_text_paragraphs() -> None:
    text = html_to_text("<p>One <em>two</em>\n three</p><p>Four<br/>five</p><ul><li>x</li></ul>")
    assert text == "One two three\n\nFour\nfive\n\n• x"


# ------------------------------------------------------------------------- toc/store


def test_toc_groups_long_novels_and_uses_volumes() -> None:
    sections = build_sections(_novel(1234).chapters, 100)
    assert len(sections) == 13
    assert sections[0].title == "Chapters 1–100" and sections[-1].title == "Chapters 1201–1234"
    assert build_sections(_novel(50).chapters, 100)[0].kind == "flat"
    vols = build_sections(_novel(1200, volumes=4).chapters, 100)
    assert [s.kind for s in vols] == ["volume"] * 4


def test_store_survives_renumbering(tmp_path) -> None:
    store = NovelStore(tmp_path)
    ch = Chapter(title="A", url="https://x.test/a", id=5)
    store.save_chapter(ch, "<p>a</p>")
    moved = Chapter(title="A", url="https://x.test/a", id=6)  # author inserted a chapter
    fresh = NovelStore(tmp_path)
    assert fresh.load_body(moved) == "<p>a</p>"
    assert [p.name[:5] for p in (tmp_path / "json").glob("*.json")] == ["00006"]


def test_safe_filename() -> None:
    assert safe_filename('A/B: "C"?') == "A B C"
    assert safe_filename("CON") == "_CON"
    assert safe_filename("...") == "novel"
