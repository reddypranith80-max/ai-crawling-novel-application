"""On-disk layout of a downloaded novel, and crash-safe file writes.

::

    <output_dir>/<Novel Title>/
        meta.json                 novel info + full chapter list
        cover.jpg                 cover image (if any)
        json/00001-1a2b3c4d.json  one file per downloaded chapter
        images/<hash>.png         images referenced by chapters
        <Novel Title>.epub        exported books
        <Novel Title>.pdf

Chapter files are named ``<id>-<url hash>``. The id keeps them sorted for
humans; the hash means a cached chapter is still found (and renamed) when
the author inserts a chapter and every later id shifts by one. Re-running a
download therefore only fetches what is genuinely missing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from novelbot.models import Chapter, Novel

log = logging.getLogger(__name__)

META_FILE = "meta.json"
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def safe_filename(name: str, *, max_length: int = 120, fallback: str = "novel") -> str:
    """Make ``name`` safe as a file/folder name on Windows, macOS and Linux."""
    name = _ILLEGAL.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if name.upper() in _RESERVED:
        name = f"_{name}"
    if len(name) > max_length:
        name = name[:max_length].rstrip(" .")
    return name or fallback


def url_hash(url: str, length: int = 8) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:length]


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write via a temp file + rename so a crash never leaves a half file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_bytes(path, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))


class NovelStore:
    """Read/write access to one novel's folder."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.chapters_dir = root / "json"
        self.images_dir = root / "images"
        self._index: dict[str, Path] | None = None  # url hash -> cached file

    # ------------------------------------------------------------- locating
    @classmethod
    def for_novel(cls, output_dir: Path, novel: Novel) -> NovelStore:
        """Pick the folder for ``novel``, avoiding clashes with other novels."""
        base = safe_filename(novel.title)
        candidate = output_dir / base
        existing = cls(candidate).load_meta()
        if existing is not None and existing.url and existing.url != novel.url:
            candidate = output_dir / safe_filename(f"{novel.title} ({novel.source})")
        return cls(candidate)

    @property
    def meta_path(self) -> Path:
        return self.root / META_FILE

    # ----------------------------------------------------------------- meta
    def save_meta(self, novel: Novel) -> None:
        atomic_write_json(self.meta_path, novel.to_dict())

    def load_meta(self) -> Novel | None:
        try:
            data = json.loads(self.meta_path.read_text("utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable %s: %s", self.meta_path, exc)
            return None
        return Novel.from_dict(data)

    # ------------------------------------------------------------- chapters
    def _chapter_name(self, chapter: Chapter) -> str:
        return f"{chapter.id:05d}-{url_hash(chapter.url)}.json"

    def _build_index(self) -> dict[str, Path]:
        index: dict[str, Path] = {}
        if self.chapters_dir.is_dir():
            for path in self.chapters_dir.glob("*.json"):
                _, _, digest = path.stem.partition("-")
                if digest:
                    index[digest] = path
        return index

    @property
    def index(self) -> dict[str, Path]:
        if self._index is None:
            self._index = self._build_index()
        return self._index

    def chapter_path(self, chapter: Chapter) -> Path | None:
        """Path of the cached chapter file, renaming it if its id changed."""
        digest = url_hash(chapter.url)
        found = self.index.get(digest)
        if found is None:
            return None
        wanted = self.chapters_dir / self._chapter_name(chapter)
        if found != wanted:
            try:
                os.replace(found, wanted)
                self.index[digest] = wanted
                found = wanted
            except OSError:
                pass
        return found if found.exists() else None

    def has_chapter(self, chapter: Chapter) -> bool:
        return self.chapter_path(chapter) is not None

    def save_chapter(self, chapter: Chapter, body: str) -> Path:
        path = self.chapters_dir / self._chapter_name(chapter)
        data = chapter.to_dict()
        data["body"] = body
        atomic_write_json(path, data)
        self.index[url_hash(chapter.url)] = path
        return path

    def load_body(self, chapter: Chapter) -> str | None:
        path = self.chapter_path(chapter)
        if path is None:
            return None
        try:
            return json.loads(path.read_text("utf-8")).get("body")
        except (OSError, ValueError) as exc:
            log.warning("Cached chapter %s is unreadable (%s); it will be re-downloaded",
                        path.name, exc)
            path.unlink(missing_ok=True)
            self.index.pop(url_hash(chapter.url), None)
            return None

    def downloaded(self, chapters: list[Chapter]) -> list[Chapter]:
        return [ch for ch in chapters if self.has_chapter(ch)]

    # ---------------------------------------------------------------- cover
    def cover_path(self) -> Path | None:
        for path in sorted(self.root.glob("cover.*")):
            if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                return path
        return None
