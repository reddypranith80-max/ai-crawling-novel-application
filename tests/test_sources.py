"""Parsing tests for the dedicated sources using small synthetic pages.

The HTML mirrors each site's structure (selectors, embedded JSON,
pagination script) but contains no real site content and no network access.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from novelbot.core.engine import Engine
from novelbot.core.selection import Selection

ROBOTS = "User-agent: *\nDisallow: /account/\n"


def serve(pages: dict[str, str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        for key, body in pages.items():
            if url == key or url.split("#")[0] == key:
                return httpx.Response(200, text=body, headers={"content-type": "text/html"})
        return httpx.Response(404, text="missing " + url)

    return httpx.MockTransport(handler)


def crawl(settings, pages: dict[str, str], url: str):
    async def flow():
        async with Engine(settings, transport=serve(pages)) as engine:
            session = await engine.open(url)
            chapters = Selection(all=True).apply(session.novel)
            result = await engine.download(session, chapters)
            bodies = {ch.id: session.store.load_body(ch) for ch in chapters}
            return session, result, bodies

    return asyncio.run(flow())


# ------------------------------------------------------------------ Royal Road
RR = "https://www.royalroad.com"
RR_CHAPTERS = [
    {"id": 11, "volumeId": 7, "title": "Prologue", "order": 0, "visible": 1,
     "url": "/fiction/999/test-story/chapter/11/prologue"},
    {"id": 12, "volumeId": 8, "title": "One", "order": 1, "visible": 1,
     "url": "/fiction/999/test-story/chapter/12/one"},
]
RR_FICTION = f"""<html><head><meta property="og:image" content="{RR}/cover.png"></head><body>
<div class="fic-header"><div class="fic-title"><h1 class="font-white">Test Story</h1>
<h4><span>by </span><span><a href="/profile/1">Some Author</a></span></h4></div></div>
<div class="fiction-info"><span class="label">ONGOING</span>
<div class="description"><div class="hidden-content"><p>Synopsis here.</p></div></div>
<span class="tags"><a class="fiction-tag">Fantasy</a><a class="fiction-tag">Magic</a></span></div>
<script>
window.chapters = {json.dumps(RR_CHAPTERS)};
window.volumes = [{{"id": 7, "title": "Book One"}}, {{"id": 8, "title": "Book Two"}}];
</script></body></html>"""
RR_CHAPTER = """<html><head><style>
.cjZhOD { display: none; speak: never; }
</style></head><body>
<div class="author-note-portlet"><div class="author-note">Thanks for reading!</div></div>
<div class="chapter-inner chapter-content"><p>Real text {n}.</p>
<p class="cjZhOD">This story was stolen from Royal Road. Please report it.</p>
<p>More real text.</p></div></body></html>"""


def test_royalroad(settings) -> None:
    pages = {
        f"{RR}/fiction/999/test-story": RR_FICTION,
        f"{RR}/fiction/999/test-story/chapter/11/prologue": RR_CHAPTER.replace("{n}", "0"),
        f"{RR}/fiction/999/test-story/chapter/12/one": RR_CHAPTER.replace("{n}", "1"),
    }
    # A chapter link is normalised to the fiction page.
    session, result, bodies = crawl(settings, pages, f"{RR}/fiction/999/test-story/chapter/12/one")
    info = session.novel.info
    assert session.crawler.name == "royalroad"
    assert (info.title, info.authors, info.status) == ("Test Story", ["Some Author"], "ongoing")
    assert info.tags == ["Fantasy", "Magic"]
    assert [(c.title, c.volume) for c in session.novel.chapters] == [
        ("Prologue", "Book One"), ("One", "Book Two")]
    assert result.ok
    assert "Real text 0." in bodies[1] and "stolen" not in bodies[1]
    assert "Thanks for reading" not in bodies[1]


# ---------------------------------------------------------------- Scribble Hub
SH = "https://www.scribblehub.com"


def sh_toc(orders: list[int]) -> str:
    items = "".join(
        f'<li class="toc_w" order="{o}"><a class="toc_a" '
        f'href="{SH}/read/555-test-tale/chapter/{1000 + o}/">Chapter {o}</a></li>'
        for o in orders
    )
    return f'<ol class="toc_ol">{items}</ol>'


SH_SERIES = f"""<html><body><div class="fic_title">Test Tale</div>
<span class="auth_name_fic">Writer X</span><div class="fic_image"><img src="{SH}/c.jpg"></div>
<div class="wi_fic_desc"><p>About the tale.</p></div>
<div class="wi_fic_genre"><a class="fic_genre">Action</a></div>
<ul><li><span class="rnd_stats"><i class="fa status"></i></span><span>Completed - Updated</span></li></ul>
<div class="wi_novel_title tags toc">Table of Contents <span class="cnt_toc">5</span></div>
{{toc}}
<script>$('#pagination-mesh-toc').pagination({{
  items: 2,
  itemsOnPage: 1 }});</script></body></html>"""
SH_CHAPTER = '<html><body><div id="chp_raw" class="chp_raw"><p>Tale text {n}.</p>' \
             '<div class="wi_authornotes">note</div></div></body></html>'


def test_scribblehub_paginated_toc_newest_first(settings) -> None:
    pages = {
        f"{SH}/series/555/test-tale/": SH_SERIES.replace("{toc}", sh_toc([5, 4, 3])),
        f"{SH}/series/555/test-tale/?toc=2": SH_SERIES.replace("{toc}", sh_toc([2, 1])),
    }
    for o in range(1, 6):
        pages[f"{SH}/read/555-test-tale/chapter/{1000 + o}/"] = SH_CHAPTER.replace("{n}", str(o))
    session, result, bodies = crawl(settings, pages, f"{SH}/read/555-test-tale/chapter/1003/")
    assert session.novel.url == f"{SH}/series/555/test-tale/"
    assert session.novel.info.status == "completed"
    assert [c.title for c in session.novel.chapters] == [f"Chapter {o}" for o in range(1, 6)]
    assert result.ok and "Tale text 5." in bodies[5] and "note" not in bodies[5]


# ------------------------------------------------------------------------- AO3
AO3 = "https://archiveofourown.org"
AO3_WORK = """<html><body><div id="workskin">
<div class="preface group"><h2 class="title heading">A Test Work</h2>
<h3 class="byline heading"><a rel="author" href="/users/x">FanAuthor</a></h3>
<div class="summary module"><blockquote class="userstuff"><p>Summary.</p></blockquote></div></div>
<div id="chapters">
 <div class="chapter" id="chapter-1"><div class="chapter preface group">
   <h3 class="title"><a href="/works/42/chapters/101">Chapter 1</a>: Start</h3>
   <div class="notes module"><p>Chapter notes.</p></div></div>
   <div class="userstuff module" role="article"><h3 class="landmark heading">Chapter Text</h3>
   <p>First chapter text.</p></div></div>
 <div class="chapter" id="chapter-2"><div class="chapter preface group">
   <h3 class="title"><a href="/works/42/chapters/102">Chapter 2</a>: End</h3></div>
   <div class="userstuff module" role="article"><p>Second chapter text.</p></div></div>
</div></div>
<dl class="work meta group"><dd class="freeform tags"><a class="tag">Fluff</a></dd>
<dd class="language" lang="en">English</dd></dl>
<dl class="stats"><dd class="chapters">2/2</dd></dl></body></html>"""


def test_ao3_single_request_for_whole_work(settings) -> None:
    pages = {f"{AO3}/works/42?view_adult=true&view_full_work=true": AO3_WORK}
    session, result, bodies = crawl(settings, pages, f"{AO3}/works/42/chapters/101")
    info = session.novel.info
    assert (info.title, info.authors, info.status, info.tags) == (
        "A Test Work", ["FanAuthor"], "completed", ["Fluff"])
    assert [c.title for c in session.novel.chapters] == ["Chapter 1: Start", "Chapter 2: End"]
    assert result.ok  # chapter pages were never requested: all came from the full-work page
    assert "First chapter text." in bodies[1] and "Chapter Text" not in bodies[1]
    assert "Chapter notes." not in bodies[1]
