"""Export downloaded chapters into single-file books: EPUB, PDF, TXT, JSON.

Every format produces **one file containing all selected chapters**, with a
table of contents where the format supports it. Only chapters present in
the cache are included; missing ones are reported by the caller.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

from novelbot.config import Settings
from novelbot.core.cleaner import html_to_text
from novelbot.core.epub import EpubBuilder
from novelbot.core.pdf import PdfBuilder
from novelbot.core.storage import NovelStore, atomic_write_bytes, safe_filename
from novelbot.core.toc import chapter_heading
from novelbot.models import Chapter, Novel, utc_now

log = logging.getLogger(__name__)


def output_name(novel: Novel, chapters: list[Chapter], ext: str) -> str:
    """``Title.epub`` for the whole novel, ``Title c1-50.epub`` for a part."""
    name = safe_filename(novel.title)
    if len(chapters) != len(novel.chapters):
        name += f" c{chapters[0].id}-{chapters[-1].id}"
    return f"{name}.{ext}"


def export_txt(novel: Novel, chapters: list[Chapter], store: NovelStore, path: Path) -> Path:
    info = novel.info
    rule = "=" * 72
    head = [info.title]
    if info.authors:
        head.append("by " + ", ".join(info.authors))
    head.append(f"Source: {info.url}")
    head.append(f"Chapters {chapters[0].id}-{chapters[-1].id} of {len(novel.chapters)}")
    parts = ["\n".join(head)]
    if info.synopsis:
        parts.append("SYNOPSIS\n\n" + html_to_text(info.synopsis))
    parts.append("CONTENTS\n\n" + "\n".join(f"{ch.id:>5}. {chapter_heading(ch)}"
                                            for ch in chapters))
    current_volume: int | None = None
    for ch in chapters:
        if ch.volume and ch.volume_id != current_volume:
            parts.append(f"{rule}\n{ch.volume.upper()}\n{rule}")
        current_volume = ch.volume_id
        body = html_to_text(store.load_body(ch) or "")
        parts.append(f"{chapter_heading(ch)}\n{'-' * min(72, len(chapter_heading(ch)))}\n\n{body}")
    atomic_write_bytes(path, ("\n\n\n".join(parts) + "\n").encode("utf-8"))
    return path


def export_json(novel: Novel, chapters: list[Chapter], store: NovelStore, path: Path) -> Path:
    data = {
        "generator": "novelbot",
        "exported_at": utc_now(),
        "source": novel.source,
        **novel.to_dict()["info"],
        "total_chapters": len(novel.chapters),
        "chapters": [],
    }
    for ch in chapters:
        body = store.load_body(ch) or ""
        data["chapters"].append({**ch.to_dict(), "html": body, "text": html_to_text(body)})
    atomic_write_bytes(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
    return path


def export_novel(
    novel: Novel,
    chapters: list[Chapter],
    store: NovelStore,
    formats: list[str],
    settings: Settings,
    *,
    out_dir: Path | None = None,
    on_status: Callable[[str], None] | None = None,
    on_chapter: Callable[[Chapter], None] | None = None,
) -> dict[str, Path]:
    """Write each requested format. Returns ``{format: path}``.

    ``chapters`` should already be filtered to the downloaded ones.
    """
    if not chapters:
        return {}
    out_dir = out_dir or store.root
    results: dict[str, Path] = {}
    for fmt in dict.fromkeys(formats):
        path = out_dir / output_name(novel, chapters, fmt)
        if on_status:
            on_status(f"Writing {fmt.upper()} ({len(chapters)} chapters)")
        if fmt == "epub":
            EpubBuilder(novel, chapters, store, settings.epub).build(path)
        elif fmt == "pdf":
            PdfBuilder(novel, chapters, store, settings.pdf,
                       toc_group_size=settings.epub.toc_group_size,
                       on_chapter=on_chapter).build(path)
        elif fmt == "txt":
            export_txt(novel, chapters, store, path)
        elif fmt == "json":
            export_json(novel, chapters, store, path)
        else:
            raise ValueError(f"Unknown format {fmt!r}")
        log.info("Wrote %s", path)
        results[fmt] = path
    return results
