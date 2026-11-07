"""End-to-end tests against the fake site (no network)."""

from __future__ import annotations

import asyncio
import json
import re
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from novelbot.config import SiteConfig
from novelbot.core.engine import Engine
from novelbot.core.selection import Selection
from novelbot.errors import BlockedError, RobotsDisallowedError
from tests.conftest import HOST, FakeSite

OPF_NS = {"opf": "http://www.idpf.org/2007/opf"}


def run_all(settings, site: FakeSite, url: str, formats=("epub",), selection=None):
    async def flow():
        async with Engine(settings, transport=site.transport()) as engine:
            session = await engine.open(url, selection=selection)
            chapters = (selection or Selection(all=True)).apply(session.novel)
            result = await engine.download(session, chapters)
            files = engine.export(session.novel, session.store, chapters, list(formats))
            return session, result, files

    return asyncio.run(flow())


def check_epub(path: Path, chapters: int) -> zipfile.ZipFile:
    zf = zipfile.ZipFile(path)
    names = zf.namelist()
    assert names[0] == "mimetype"
    assert zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
    assert zf.read("mimetype") == b"application/epub+zip"
    for name in names:
        if name.endswith((".xhtml", ".opf", ".ncx", ".xml")):
            etree.fromstring(zf.read(name))  # every document is well-formed XML
    opf = etree.fromstring(zf.read("OEBPS/content.opf"))
    items = opf.findall(".//opf:manifest/opf:item", OPF_NS)
    ids = {item.get("id") for item in items}
    for item in items:
        assert f"OEBPS/{item.get('href')}" in names, item.get("href")
    for ref in opf.findall(".//opf:spine/opf:itemref", OPF_NS):
        assert ref.get("idref") in ids
    assert sum(1 for n in names if re.fullmatch(r"OEBPS/text/c\d{5}\.xhtml", n)) == chapters
    return zf


def test_1200_chapters_become_one_epub_pdf_txt_json(settings, make_site) -> None:
    site = make_site(chapters=1200)
    session, result, files = run_all(settings, site, f"{HOST}/novel/big/",
                                     formats=("epub", "pdf", "txt", "json"))
    assert session.crawler.name == "generic"
    assert session.novel.title == "Big Story"
    assert session.novel.info.authors == ["Jane Writer"]
    assert len(session.novel.chapters) == 1200  # the "latest elsewhere" nav link is ignored
    assert result.ok and result.downloaded == 1200
    assert set(files) == {"epub", "pdf", "txt", "json"}
    assert all(p.name.startswith("Big Story.") for p in files.values())

    zf = check_epub(files["epub"], 1200)
    nav = zf.read("OEBPS/nav.xhtml").decode()
    assert nav.count('href="text/c') >= 1200
    assert nav.count("Chapters 1101–1200") == 1  # grouped into blocks of 100
    assert any(n.startswith("OEBPS/images/cover.") for n in zf.namelist())
    chapter3 = zf.read("OEBPS/text/c00003.xhtml").decode()
    assert re.search(r'<img [^>]*src="\.\./images/[0-9a-f]+\.png"', chapter3)  # embedded
    chapter1 = zf.read("OEBPS/text/c00001.xhtml").decode()
    assert chapter1.count("Chapter 1: Title 1") == 2  # <title> + <h2>; duplicate body line removed
    assert "BUY STUFF" not in chapter1 and "alert" not in chapter1 and "café" in chapter1

    pdf = files["pdf"].read_bytes()
    assert pdf.startswith(b"%PDF") and b"/Outlines" in pdf
    assert pdf.count(b"/Type /Page\n") + pdf.count(b"/Type /Page ") >= 1200

    text = files["txt"].read_text("utf-8")
    assert "Chapter 1200: Title 1200" in text and "CONTENTS" in text

    data = json.loads(files["json"].read_text("utf-8"))
    assert len(data["chapters"]) == 1200 and data["title"] == "Big Story"


def test_resume_skips_saved_chapters(settings, make_site) -> None:
    site = make_site(chapters=6)
    run_all(settings, site, f"{HOST}/novel/big/")
    before = site.requests["/novel/big/chapter-2/"]
    _, result, _ = run_all(settings, site, f"{HOST}/novel/big/")
    assert result.downloaded == 0 and result.cached == 6
    assert site.requests["/novel/big/chapter-2/"] == before


def test_follow_next_links_from_first_chapter(settings, make_site) -> None:
    site = make_site(chapters=7)
    session, result, files = run_all(settings, site, f"{HOST}/novel/big/chapter-1/")
    titles = [c.title for c in session.novel.chapters]
    assert titles == [f"Chapter {i}: Title {i}" for i in range(1, 8)]
    assert session.novel.title == "Big Story"
    assert result.downloaded == 7
    # Each chapter page was fetched once (bodies were prefetched while following links).
    assert all(site.requests[f"/novel/big/chapter-{i}/"] == 1 for i in range(1, 8))
    check_epub(files["epub"], 7)


def test_failed_chapters_are_reported_and_rest_exported(settings, make_site) -> None:
    site = make_site(chapters=10)
    site.fail = {4, 9}
    _, result, files = run_all(settings, site, f"{HOST}/novel/big/", formats=("epub", "txt"))
    assert sorted(ch.id for ch, _ in result.failed) == [4, 9]
    assert result.downloaded == 8
    check_epub(files["epub"], 8)


def test_partial_range_file_name(settings, make_site) -> None:
    site = make_site(chapters=30)
    _, _, files = run_all(settings, site, f"{HOST}/novel/big/", selection=Selection(ranges="5-12"))
    assert files["epub"].name == "Big Story c5-12.epub"
    check_epub(files["epub"], 8)


def test_descending_index_is_reversed(settings, make_site) -> None:
    site = make_site(chapters=5)
    site.descending = True
    session, _, _ = run_all(settings, site, f"{HOST}/novel/big/")
    assert session.novel.chapters[0].title == "Chapter 1: Title 1"


def test_robots_txt_is_respected(settings, make_site) -> None:
    site = make_site(chapters=3)
    with pytest.raises(RobotsDisallowedError):
        run_all(settings, site, f"{HOST}/private/novel/")
    assert site.requests["/private/novel/"] == 0


def test_cloudflare_challenge_stops_without_retrying(settings, make_site) -> None:
    site = make_site(chapters=3)
    site.block = True
    settings.http.retries = 5
    with pytest.raises(BlockedError):
        run_all(settings, site, f"{HOST}/novel/big/")
    assert site.requests["/novel/big/"] == 1


def test_config_selectors(settings, make_site) -> None:
    settings.sites["fake"] = SiteConfig(
        name="fake", hosts=["novels.test"], title="h1", chapter_list="ul.chapter-list a",
        content="div.chapter-content", bad_text=["read the latest chapters"],
    )
    site = make_site(chapters=4)
    session, result, files = run_all(settings, site, f"{HOST}/novel/big/")
    assert session.crawler.display_name == "fake (config)"
    assert result.downloaded == 4
    body = check_epub(files["epub"], 4).read("OEBPS/text/c00002.xhtml").decode()
    assert "spam-site" not in body
