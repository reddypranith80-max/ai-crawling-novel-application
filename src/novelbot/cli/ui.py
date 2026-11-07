"""Terminal presentation: logging, progress bars, summaries (rich)."""

from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from novelbot.core.downloader import DownloadResult
from novelbot.models import Chapter, Novel

console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)


def setup_logging(verbose: int = 0, quiet: bool = False, log_file: Path | None = None) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    app_level = logging.WARNING if quiet else (logging.DEBUG if verbose else logging.INFO)
    handler = RichHandler(console=err_console, show_time=False, show_path=False,
                          markup=False, rich_tracebacks=True)
    handler.setLevel(app_level)
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    logging.getLogger("novelbot").setLevel(logging.DEBUG)
    lib_level = logging.DEBUG if verbose >= 2 else logging.WARNING
    for name in ("httpx", "httpcore", "fpdf", "fontTools", "PIL", "asyncio"):
        logging.getLogger(name).setLevel(lib_level)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(file_handler)


class RichProgress:
    """Implements the downloader's ProgressHooks with a rich progress bar."""

    def __init__(self, out: Console | None = None) -> None:
        self.console = out or err_console
        self._progress: Progress | None = None
        self._task: TaskID | None = None
        self.failed = 0

    def _make(self) -> Progress:
        return Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("[red]{task.fields[failed]}"),
            TimeElapsedColumn(),
            TextColumn("eta"),
            TimeRemainingColumn(),
            console=self.console,
        )

    def start(self, total: int, description: str) -> None:
        self._progress = self._make()
        self._progress.start()
        self._task = self._progress.add_task(description, total=total, failed="")

    def advance(self, chapter: Chapter, ok: bool) -> None:
        if not ok:
            self.failed += 1
        if self._progress is not None and self._task is not None:
            self._progress.update(self._task, advance=1,
                                  failed=f"{self.failed} failed" if self.failed else "")

    def finish(self) -> None:
        if self._progress is not None:
            self._progress.stop()
            self._progress = None


def novel_panel(novel: Novel) -> Panel:
    info = novel.info
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold cyan", no_wrap=True)
    grid.add_column()
    grid.add_row("Title", info.title)
    if info.authors:
        grid.add_row("Author", ", ".join(info.authors))
    grid.add_row("Source", f"{novel.source}  {info.url}")
    volumes = novel.volumes
    count = f"{len(novel.chapters)}"
    if len(volumes) > 1:
        count += f" in {len(volumes)} volumes"
    grid.add_row("Chapters", count)
    if info.status:
        grid.add_row("Status", info.status)
    if info.tags:
        tags = ", ".join(info.tags[:12]) + (" ..." if len(info.tags) > 12 else "")
        grid.add_row("Tags", tags)
    return Panel(grid, title="Novel", border_style="cyan", expand=False)


def download_summary(result: DownloadResult) -> str:
    parts = [f"[green]{result.downloaded} downloaded[/]"]
    if result.cached:
        parts.append(f"{result.cached} already saved")
    if result.failed:
        parts.append(f"[red]{len(result.failed)} failed[/]")
    return ", ".join(parts)


def failures_table(result: DownloadResult, limit: int = 15) -> Table:
    table = Table(title="Failed chapters", title_style="bold red", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("Chapter")
    table.add_column("Reason", style="red")
    for chapter, reason in result.failed[:limit]:
        table.add_row(str(chapter.id), chapter.title, reason)
    if len(result.failed) > limit:
        table.add_row("", f"... and {len(result.failed) - limit} more", "")
    return table


def outputs_table(files: dict[str, Path]) -> Table:
    table = Table(title="Saved", title_style="bold green", show_header=False, box=None,
                  padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_column(justify="right", style="dim")
    for fmt, path in files.items():
        size = path.stat().st_size if path.exists() else 0
        table.add_row(fmt.upper(), str(path.resolve()), _human_size(size))
    return table


def _human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
