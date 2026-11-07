"""The crawl -> download -> export flow shared by every CLI entry point."""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn
from rich.prompt import IntPrompt, Prompt

from novelbot.cli import ui
from novelbot.config import Settings, validate
from novelbot.core.engine import Engine, NovelSession, load_local
from novelbot.core.selection import Selection, SelectionError, parse_ranges
from novelbot.errors import NovelBotError
from novelbot.models import Chapter, Novel

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_PARTIAL = 3
EXIT_BLOCKED = 4
EXIT_INTERRUPTED = 130


@dataclass
class CrawlRequest:
    url: str
    selection: Selection = field(default_factory=Selection)
    formats: list[str] | None = None
    source: str | None = None
    force: bool = False
    #: Ask for a chapter range when none was given (only on a terminal).
    prompt_selection: bool = True


def can_prompt() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def ask_selection(novel: Novel) -> Selection:
    """Interactively choose which chapters to download."""
    total = len(novel.chapters)
    volumes = novel.volumes
    choices = ["all", "first", "last", "range"] + (["volumes"] if len(volumes) > 1 else [])
    ui.console.print(f"\n[bold]{total}[/] chapters available"
                     + (f" in {len(volumes)} volumes" if len(volumes) > 1 else ""))
    mode = Prompt.ask("Which chapters?", choices=choices, default="all", console=ui.console)
    if mode == "all":
        return Selection(all=True)
    if mode in ("first", "last"):
        while True:
            n = IntPrompt.ask(f"How many ({mode})?", default=min(50, total), console=ui.console)
            if n >= 1:
                return Selection(first=n) if mode == "first" else Selection(last=n)
    if mode == "volumes":
        for i, vol in enumerate(volumes, 1):
            ui.console.print(f"  {i:>3}. {vol.title} [dim]({len(vol.chapter_ids)} chapters)[/]")
    label = "Volumes (e.g. 1,3-4)" if mode == "volumes" else f"Chapters (e.g. 1-100,150 of {total})"
    limit = len(volumes) if mode == "volumes" else total
    while True:
        spec = Prompt.ask(label, console=ui.console)
        try:
            if parse_ranges(spec, limit):
                return Selection(volumes=spec) if mode == "volumes" else Selection(ranges=spec)
        except SelectionError as exc:
            ui.console.print(f"[red]{exc}[/]")
            continue
        ui.console.print("[red]That selects nothing; try again.[/]")


def ask_formats(default: list[str]) -> list[str]:
    from novelbot.config import SUPPORTED_FORMATS

    while True:
        answer = Prompt.ask(f"Formats ({', '.join(SUPPORTED_FORMATS)}; comma separated)",
                            default=",".join(default), console=ui.console)
        formats = split_formats([answer])
        bad = [f for f in formats if f not in SUPPORTED_FORMATS]
        if formats and not bad:
            return formats
        ui.console.print(f"[red]Unknown format(s): {', '.join(bad) or '(none)'}[/]")


def split_formats(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        out += [v.strip().lower().lstrip(".") for v in value.split(",") if v.strip()]
    return list(dict.fromkeys(out))


def run(coro_factory) -> int:  # type: ignore[no-untyped-def]
    """Run an async flow with friendly handling of errors and Ctrl+C."""
    try:
        return asyncio.run(coro_factory())
    except KeyboardInterrupt:
        ui.err_console.print("\n[yellow]Interrupted.[/] Everything downloaded so far is saved; "
                             "run the same command again to resume.")
        return EXIT_INTERRUPTED
    except NovelBotError as exc:
        ui.err_console.print(f"[bold red]Error:[/] {exc}")
        return EXIT_ERROR


async def crawl(settings: Settings, request: CrawlRequest) -> int:
    if request.formats:
        settings.download.formats = request.formats
    validate(settings)  # fail fast, before any network traffic
    async with Engine(settings) as engine:
        with ui.err_console.status("Connecting...") as status:
            session = await engine.open(request.url, source=request.source,
                                        selection=request.selection,
                                        on_status=lambda m: status.update(m))
        ui.console.print(ui.novel_panel(session.novel))

        selection = request.selection
        if selection.is_empty and request.prompt_selection and can_prompt():
            selection = ask_selection(session.novel)
        chapters = selection.apply(session.novel)
        formats = settings.download.formats

        ui.console.print(f"Selected [bold]{len(chapters)}[/] chapter(s) ({selection.describe()}) "
                         f"-> [bold]{', '.join(f.upper() for f in formats)}[/]  "
                         f"[dim]{session.store.root.resolve()}[/]")
        progress = ui.RichProgress()
        result = await engine.download(session, chapters, force=request.force, progress=progress)
        ui.console.print(ui.download_summary(result))
        if result.failed:
            ui.console.print(ui.failures_table(result))
        if result.aborted:
            ui.err_console.print(f"[bold red]Stopped:[/] {result.aborted}")

        export(engine, session, chapters, formats)
        if result.aborted:
            return EXIT_BLOCKED
        return EXIT_PARTIAL if result.failed else EXIT_OK


def export(engine: Engine, session: NovelSession, chapters: list[Chapter],
           formats: list[str]) -> dict[str, Path]:
    return export_with_progress(engine, session.novel, session.store, chapters, formats)


def export_with_progress(engine: Engine, novel: Novel, store, chapters: list[Chapter],  # type: ignore[no-untyped-def]
                         formats: list[str]) -> dict[str, Path]:
    available = store.downloaded(chapters)
    if not available:
        ui.err_console.print("[yellow]No downloaded chapters to export.[/]")
        return {}
    with Progress(SpinnerColumn(), TextColumn("[bold]{task.description}"), BarColumn(),
                  MofNCompleteColumn(), console=ui.err_console, transient=True) as progress:
        task = progress.add_task("Exporting", total=None)

        def on_status(message: str) -> None:
            total = len(available) if "PDF" in message else None
            progress.update(task, description=message, completed=0, total=total)

        files = engine.export(novel, store, available, formats, on_status=on_status,
                              on_chapter=lambda _ch: progress.advance(task))
    if files:
        ui.console.print(ui.outputs_table(files))
    return files


async def export_local(settings: Settings, folder: Path, selection: Selection,
                       formats: list[str] | None) -> int:
    """Rebuild books from an already-downloaded folder (no network)."""
    if formats:
        settings.download.formats = formats
    validate(settings)
    novel, store = load_local(folder)
    ui.console.print(ui.novel_panel(novel))
    chapters = selection.apply(novel)
    async with Engine(settings) as engine:
        files = export_with_progress(engine, novel, store, chapters,
                                     formats or settings.download.formats)
    return EXIT_OK if files else EXIT_ERROR


def novel_url_from_folder(folder: Path) -> str:
    novel, _ = load_local(folder)
    if not novel.url:
        raise NovelBotError(f"{folder} has no source URL in meta.json")
    return novel.url
