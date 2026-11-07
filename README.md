# AI Crawling Novel Application

A Python web novel crawler: paste a novel's **website link** and get the whole novel
as **one all-in-one PDF or EPUB** (or TXT / JSON) saved on your own device, whether it
has 10 chapters or 3,000.

It is built on a small async engine with a plugin-style crawler interface, and it downloads
politely: it follows robots.txt, waits between requests to the same site, and retries
with backoff. It ships as the `novelbot` command.

```console
$ novelbot crawl https://www.scribblehub.com/series/123456/some-novel/ -f epub,pdf --all
╭──────────────────── Novel ─────────────────────╮
│ Title     Some Novel                           │
│ Author    Some Author                          │
│ Source    scribblehub  https://www.scribbl...  │
│ Chapters  1240                                 │
╰────────────────────────────────────────────────╯
Selected 1240 chapter(s) (all chapters) -> EPUB, PDF
⠋ Downloading ━━━━━━━━━━━━━━━━━━━━━━━━  612/1240  0:20:31 eta 0:21:02
```

## Features

- **One file per format, any length.** 1,000+ chapters go into one EPUB/PDF with a full
  table of contents. Long novels without volumes get their contents grouped into blocks
  of 100 chapters ("Chapters 1–100", "101–200", …) so the contents stay navigable.
  Volumes and arcs become top-level sections.
- **Formats.** EPUB 3 (with an EPUB 2 NCX for older readers), PDF (clickable contents,
  bookmarks, Unicode and CJK fonts), TXT and JSON.
- **Any website.** There are dedicated crawlers for Royal Road, Scribble Hub and Archive of
  Our Own. Any other site is handled by a generic crawler: describe the site with CSS
  selectors in your config, or let the built-in heuristics find the chapter list. You can
  even give it the URL of *chapter 1* and it will follow "Next chapter" links to the end.
- **Resume and update.** Every chapter is saved as soon as it is downloaded. Re-run the
  same command after a crash, Ctrl+C or network failure and only the missing chapters
  are fetched. `novelbot update <folder>` picks up newly published chapters.
- **Polite by default.** It obeys robots.txt (RFC 9309: wildcards, longest match,
  Crawl-delay), waits between requests to the same site, retries with exponential
  backoff and honours `Retry-After`. If a site blocks automated access (Cloudflare
  challenge, HTTP 418), it stops cleanly and never tries to bypass the protection.
- **Chapter selection.** `--all`, `--first 50`, `--last 10`, `--range "1-100,150,200-"`,
  `--volumes 2-3`.
- **Images.** Chapter images and the cover are downloaded and embedded, so books work offline.
- **Easy to extend.** A new site is one Python class with three methods, or a few lines of TOML.

## Installation

Requires **Python 3.11+**.

```bash
git clone https://github.com/reddypranith80-max/ai-crawling-novel-application.git
cd ai-crawling-novel-application

# with uv
uv sync
uv run novelbot --help

# or with pip
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux
pip install -e .
novelbot --help
```

This installs the `novelbot` command (`python -m novelbot` works too).

## Quick start

```bash
# Guided mode: paste a link, pick chapters and formats
novelbot

# Whole novel as one EPUB
novelbot crawl https://www.royalroad.com/fiction/12345/some-story -f epub --all

# EPUB and PDF of the first 100 chapters
novelbot crawl URL -f epub,pdf --first 100

# Everything from chapter 200 onwards, as PDF
novelbot crawl URL -f pdf --range 200-

# Any other site: give the novel's index page...
novelbot crawl https://example-novels.com/novel/my-novel/ -f epub --all
# ...or its first chapter, and novelbot follows the "Next" links
novelbot crawl https://example-novels.com/my-novel/chapter-1/ -f epub --all

# Just look at a novel
novelbot info URL --chapters
```

Books are written to `./Novels/<Title>/`:

```
Novels/
└── Some Story/
    ├── Some Story.epub        <- one file, all chapters, full table of contents
    ├── Some Story.pdf
    ├── meta.json              <- novel info + chapter list
    ├── cover.jpg
    ├── images/                <- chapter images
    └── json/00001-1a2b3c4d.json ...   <- one cached file per chapter (resume)
```

A partial download is named after its range, e.g. `Some Story c1-100.epub`.

## Commands

| Command | What it does |
|---|---|
| `novelbot` / `novelbot interactive` | Guided mode with prompts. |
| `novelbot crawl URL [options]` | Download and export. Re-run it to resume. |
| `novelbot info URL [-l]` | Show details and chapter count (`-l` lists chapters). Writes nothing. |
| `novelbot update FOLDER` | Re-check the site for new chapters, download them, rebuild the books. |
| `novelbot export FOLDER -f pdf` | Rebuild books from downloaded chapters, offline. |
| `novelbot sources` | List supported sites. |
| `novelbot config init / show / path` | Create, inspect or locate the config file. |

Useful `crawl` options:

| Option | Meaning |
|---|---|
| `-f, --format` | `epub`, `pdf`, `txt`, `json`. Repeat or comma-separate. |
| `-a, --all` / `--first N` / `--last N` / `-r, --range SPEC` / `--volumes SPEC` | Chapter selection. With none given you are asked (or everything is used with `-y`). |
| `-o, --output DIR` | Where novel folders are created (default `./Novels`). |
| `-j, --concurrency N` | Parallel chapter downloads (the per-site delay still applies). |
| `--delay SECONDS` | Minimum time between requests to the same site. |
| `--force` | Re-download chapters already saved. |
| `--no-images` | Skip chapter images. |
| `-s, --source NAME` | Force a crawler, e.g. `generic`. |
| `--ignore-robots` | Skip robots.txt checks. Only for sites you have permission to download from. |
| `-y, --yes` | Never prompt. |
| `-v` / `-vv` / `-q` | More or less output. |

Exit codes: `0` success, `1` error, `3` finished but some chapters failed, `4` the site
blocked automated access, `130` interrupted (everything saved so far is kept).

## Supported sources

| Name | Site | Notes |
|---|---|---|
| `royalroad` | royalroad.com | Volumes, author-note removal, hidden anti-piracy paragraphs removed. |
| `scribblehub` | scribblehub.com | Paginated chapter index, 2 s minimum delay. |
| `ao3` | archiveofourown.org | Whole work in one request; works for logged-in users only are reported. |
| `generic` | any site | Config selectors, index-page heuristics, or follow "Next chapter" links. |

Royal Road, Scribble Hub and AO3 use bot protection that sometimes refuses automated
clients, depending on your network. novelbot then stops with a "blocked" message instead
of hammering the site or trying to get around the protection. Waiting a while or using
another network usually helps.

## Teaching novelbot a new site with TOML (no code)

Create a config file (`novelbot config init` writes a commented one) and add a
`[sites.<name>]` table:

```toml
[sites.example]
hosts = ["www.example-novels.com", "example-novels.com"]
title = "h1.novel-title"
author = ".author a"
cover = ".novel-cover img"
synopsis = ".summary"
chapter_list = "ul.chapter-list li a"   # chapter links on the index page
toc_next = "a.next-page"                # if the index is paginated
reverse_chapters = false                # true if the index lists newest first
content = "div.chapter-content"
remove = [".ads", ".share-buttons"]
bad_text = ["Read the latest chapters at"]
delay = 2.0

# No chapter index at all? Leave out chapter_list, pass the URL of chapter 1 and say
# how to find the next chapter:
# next_chapter = "a.next-chapter"
```

Without a `[sites]` entry the generic crawler still tries: it finds the block of links
that look like chapters, finds the chapter text by looking for the element with the
most paragraph text, and follows `rel="next"` / "Next chapter" links when there is no
index.

## Adding a new source in Python

Create `src/novelbot/sources/mysite.py` (picked up automatically), or put the file in a
folder listed under `plugin_dirs` in your config:

```python
from novelbot.models import Chapter, NovelInfo
from novelbot.sources.base import BaseCrawler


class MySiteCrawler(BaseCrawler):
    name = "mysite"                       # used by --source and meta.json
    display_name = "My Site"
    base_urls = ("https://www.mysite.com/",)
    min_delay = 1.0                       # seconds between requests (optional)
    remove_selectors = (".ads",)          # stripped from every chapter (optional)

    async def get_info(self, url: str) -> NovelInfo:
        soup = await self.get_soup(url, cache=True)   # cache: get_chapters reuses it
        return NovelInfo(
            title=self.text(soup, "h1"),
            url=url,
            authors=self.texts(soup, ".author a"),
            cover_url=self.meta(soup, "og:image"),
            synopsis=str(soup.select_one(".synopsis") or ""),
        )

    async def get_chapters(self, url: str) -> list[Chapter]:
        soup = await self.get_soup(url, cache=True)
        return [
            Chapter(title=a.get_text(strip=True), url=self.absolute_url(a["href"]),
                    volume=None)          # or a volume title, to group chapters
            for a in soup.select(".chapter-list a")
        ]                                 # oldest first

    async def get_chapter_content(self, chapter: Chapter):
        soup = await self.get_soup(chapter.url)
        return self.select_one_or_fail(soup, ".chapter-content", chapter.url)
```

Optional hooks: `normalize_url(url)` maps chapter links to the novel page, and
`async initialize()` runs once before the first request. Chapter HTML is cleaned
automatically (scripts, ads, styles and unsafe attributes removed; lazy images resolved).
Installable plugins can register under the `novelbot.sources` entry-point group.

## Configuration

Settings come from (first match wins) `--config PATH`, `$NOVELBOT_CONFIG`,
`./novelbot.toml`, then the per-user file (`novelbot config path`). Command-line options
override the file. See
[src/novelbot/example_config.toml](src/novelbot/example_config.toml) for every key.
Highlights:

```toml
[download]
formats = ["epub", "pdf"]
concurrency = 3

[http]
delay = 1.5            # seconds between requests to the same site
respect_robots = true

[epub]
toc_group_size = 100   # contents grouping for long novels without volumes

[pdf]
page_size = "A5"       # A4, A5, A6, Letter, Legal
font_size = 11
# font = "C:/Windows/Fonts/georgia.ttf"   # auto-detected when omitted
```

## Project layout

```
src/novelbot/
├── models.py         NovelInfo, Chapter, Volume, Novel
├── config.py         settings dataclasses + TOML loading
├── errors.py         user-facing exceptions
├── core/
│   ├── http.py       async httpx client: robots, rate limits, retries, block detection
│   ├── robots.py     RFC 9309 robots.txt parser
│   ├── ratelimit.py  per-host pacing with adaptive slow-down
│   ├── engine.py     URL -> crawler -> metadata -> download -> export
│   ├── downloader.py concurrent chapter downloads with resume
│   ├── cleaner.py    HTML sanitising, HTML -> text
│   ├── storage.py    on-disk layout, atomic writes
│   ├── images.py     image + cover downloads
│   ├── selection.py  --first/--last/--range/--volumes
│   ├── toc.py        contents structure (volumes / 100-chapter groups)
│   ├── epub.py       EPUB 3 builder
│   ├── pdf.py        PDF builder (fpdf2)
│   └── export.py     format dispatch, TXT and JSON writers
├── sources/          base.py (BaseCrawler) + one module per site
└── cli/              Typer app, runner, rich UI
```

## Development

```bash
pip install -e ".[dev]"      # or: uv sync
pytest                       # 37 tests, no network access needed
ruff check src tests
```

The test suite includes a fake 1,200-chapter website (served through
`httpx.MockTransport`) that checks the whole pipeline: discovery, concurrent download,
resume, failures, robots.txt, block detection and single-file EPUB/PDF/TXT/JSON output.

## Responsible use

novelbot is meant for personal offline reading of content you can already read for
free in a browser. Respect authors and sites: keep the default delays, don't share
downloaded books, and follow each site's terms of service. Many web novel authors
depend on site views and support; consider following and supporting them.
