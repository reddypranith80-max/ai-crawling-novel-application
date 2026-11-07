"""Shared fixtures: a fake novel website served through httpx.MockTransport.

No test touches the network. The fake site ``novels.test`` has:

* ``/robots.txt``                     disallows ``/private/``
* ``/novel/big/``                     index page with N chapter links
* ``/novel/big/chapter-<i>/``         chapter pages (with scripts, ads, a "Next" link)
* ``/img/pic.png``, ``/cover.jpg``    real images generated with Pillow
"""

from __future__ import annotations

import io
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from PIL import Image

from novelbot.config import Settings

HOST = "https://novels.test"


def _image(fmt: str, size: tuple[int, int] = (40, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 80, 40)).save(buf, fmt)
    return buf.getvalue()


PNG = _image("PNG")
JPEG = _image("JPEG", (300, 450))


class FakeSite:
    """Configurable fake site; records every request path."""

    def __init__(self, chapters: int = 5) -> None:
        self.chapters = chapters
        self.requests: Counter[str] = Counter()
        self.fail: set[int] = set()        # chapter numbers returning 404
        self.block = False                 # serve a Cloudflare challenge
        self.descending = False            # index lists newest first
        self.image_in: set[int] = {3}      # chapters that contain an image

    # -- pages ------------------------------------------------------------
    def index(self) -> str:
        order = range(self.chapters, 0, -1) if self.descending else range(1, self.chapters + 1)
        links = "".join(
            f'<li><a href="/novel/big/chapter-{i}/">Chapter {i}: Title {i}</a></li>' for i in order
        )
        return f"""<html><head><title>Big Story | Novels Test</title>
<meta property="og:title" content="Big Story">
<meta property="og:image" content="/cover.jpg">
<meta property="og:description" content="A very long story &amp; more.">
<meta name="author" content="Jane Writer"></head>
<body><nav><a href="/">Home</a><a href="/latest/chapter-99/">Chapter 99 (latest elsewhere)</a></nav>
<h1>Big Story</h1><div class="toc"><ul class="chapter-list">{links}</ul></div>
<footer>footer</footer></body></html>"""

    def chapter(self, i: int) -> str:
        nxt = f'<a class="btn" href="/novel/big/chapter-{i + 1}/">Next Chapter</a>' \
            if i < self.chapters else ""
        img = '<p><img data-src="/img/pic.png" src="data:image/gif;base64,R0lGOD" alt="pic"></p>' \
            if i in self.image_in else ""
        return f"""<html><head><title>Big Story - Chapter {i}</title>
<script>var tracking = 1;</script></head><body>
<h1>Chapter {i}: Title {i}</h1>
<div class="chapter-content">
  <p>Chapter {i}: Title {i}</p>
  <p>This is the <em>first</em> paragraph of chapter {i}. {"Lorem ipsum dolor sit amet. " * 8}</p>
  <div class="adsbygoogle">BUY STUFF</div>
  <script>alert('x')</script>
  {img}
  <p style="color:red" onclick="evil()">Second paragraph with <span class="x">a span</span> and
  unicode: café — “quotes” … 東京.</p>
  <p>   </p>
  <p>Read the latest chapters at spam-site dot com</p>
</div>
<div class="nav">{nxt}</div></body></html>"""

    # -- transport ----------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests[path] += 1
        if self.block and path != "/robots.txt":
            return httpx.Response(
                403, headers={"content-type": "text/html", "cf-mitigated": "challenge"},
                text="<html><head><title>Just a moment...</title></head></html>",
            )
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
        if path == "/cover.jpg":
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        if path == "/img/pic.png":
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        if path == "/novel/big/":
            return httpx.Response(200, text=self.index(), headers={"content-type": "text/html"})
        if path.startswith("/novel/big/chapter-"):
            number = int(path.rstrip("/").rsplit("-", 1)[1])
            if number in self.fail or not 1 <= number <= self.chapters:
                return httpx.Response(404, text="not found")
            return httpx.Response(200, text=self.chapter(number),
                                  headers={"content-type": "text/html"})
        return httpx.Response(404, text="not found")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.download.output_dir = tmp_path / "Novels"
    s.download.concurrency = 8
    s.http.delay = 0
    s.http.jitter = 0
    s.http.retries = 1
    s.http.backoff = 0.01
    return s


@pytest.fixture
def make_site() -> Callable[..., FakeSite]:
    return FakeSite
