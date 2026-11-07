"""Download chapter images and covers so the books work fully offline.

Chapter HTML is rewritten from ``<img src="https://cdn/...">`` to
``<img src="images/<hash>.png">`` (relative to the novel folder). Images
that cannot be fetched are replaced by their alt text.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from pathlib import Path

from bs4 import BeautifulSoup

from novelbot.core.http import HttpClient
from novelbot.core.storage import atomic_write_bytes, url_hash
from novelbot.errors import NovelBotError

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 15 * 1024 * 1024
MEDIA_TYPES = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}
_CONTENT_TYPE_EXT = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/pjpeg": "jpg", "image/png": "png",
    "image/gif": "gif", "image/webp": "webp",
}


def sniff_extension(data: bytes, content_type: str = "") -> str | None:
    """Detect a supported image type from magic bytes (falling back to Content-Type)."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return _CONTENT_TYPE_EXT.get(content_type.split(";")[0].strip().lower())


class ImageCache:
    def __init__(self, http: HttpClient, images_dir: Path) -> None:
        self.http = http
        self.images_dir = images_dir
        self._pending: dict[str, asyncio.Task[str | None]] = {}

    async def fetch(self, url: str) -> str | None:
        """Return ``images/<file>`` for ``url``, downloading it once."""
        digest = url_hash(url, 16)
        existing = next(self.images_dir.glob(f"{digest}.*"), None) if self.images_dir.is_dir() \
            else None
        if existing is not None:
            return f"images/{existing.name}"
        task = self._pending.get(digest)
        if task is None:
            task = asyncio.ensure_future(self._download(url, digest))
            self._pending[digest] = task
        return await task

    async def _download(self, url: str, digest: str) -> str | None:
        try:
            if url.startswith("data:"):
                data, content_type = _decode_data_uri(url)
            else:
                data, content_type = await self.http.get_bytes(url)
        except NovelBotError as exc:
            log.debug("Image %s skipped: %s", url, exc)
            return None
        if not data or len(data) > MAX_IMAGE_BYTES:
            return None
        ext = sniff_extension(data, content_type)
        if ext is None:
            log.debug("Image %s has unsupported type %r", url, content_type)
            return None
        atomic_write_bytes(self.images_dir / f"{digest}.{ext}", data)
        return f"images/{digest}.{ext}"

    async def localize(self, html: str) -> str:
        """Download every image in ``html`` and point ``src`` at the local copy."""
        if "<img" not in html:
            return html
        soup = BeautifulSoup(html, "lxml")
        root = soup.body or soup
        images = root.find_all("img")
        results = await asyncio.gather(*(self.fetch(str(img.get("src", ""))) for img in images))
        for img, local in zip(images, results, strict=True):
            if local:
                img["src"] = local
            else:
                alt = str(img.get("alt") or "").strip()
                img.replace_with(f"[{alt}]" if alt else "")
        return "".join(str(c) for c in root.contents)


async def download_cover(http: HttpClient, url: str, novel_dir: Path) -> Path | None:
    """Save the cover as ``cover.<ext>`` in ``novel_dir``. Returns None on failure."""
    try:
        data, content_type = await http.get_bytes(url)
    except NovelBotError as exc:
        log.warning("Could not download cover: %s", exc)
        return None
    ext = sniff_extension(data, content_type)
    if ext is None or len(data) > MAX_IMAGE_BYTES:
        log.warning("Cover at %s is not a supported image", url)
        return None
    for old in novel_dir.glob("cover.*"):
        old.unlink(missing_ok=True)
    path = novel_dir / f"cover.{ext}"
    atomic_write_bytes(path, data)
    return path


def _decode_data_uri(uri: str) -> tuple[bytes, str]:
    header, _, payload = uri.partition(",")
    content_type = header[5:].split(";")[0]
    if ";base64" in header:
        try:
            return base64.b64decode(payload), content_type
        except (binascii.Error, ValueError):
            return b"", content_type
    return payload.encode("latin-1", "ignore"), content_type
