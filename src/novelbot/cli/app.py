"""``novelbot`` command-line interface."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from novelbot import __version__
from novelbot.cli import runner, ui
from novelbot.config import (
    SUPPORTED_FORMATS,
    Settings,
    find_config_file,
    load_settings,
    user_config_file,
)
from novelbot.core.engine import Engine
from novelbot.core.selection import Selection
from novelbot.errors import NovelBotError

app = typer.Typer(
    name="novelbot",
    help="Download web novels from their public web pages into one EPUB / PDF / TXT / JSON "
         "file on your own device.",
    rich_markup_mode="rich",
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
config_app = typer.Typer(help="Show, locate or create the configuration file.")
app.add_typer(config_app, name="config")

# --------------------------------------------------------------------------
# Shared option types
# --------------------------------------------------------------------------
ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="Path to a config TOML file.",
                                                 exists=True, dir_okay=False)]
VerboseOpt = Annotated[int, typer.Option("--verbose", "-v", count=True,
                                         help="More output (-vv also logs HTTP).")]
QuietOpt = Annotated[bool, typer.Option("--quiet", "-q", help="Only show warnings and errors.")]
FormatOpt = Annotated[list[str] | None, typer.Option(
    "--format", "-f",
    help=f"Output format(s): {', '.join(SUPPORTED_FORMATS)}. Repeat or comma-separate "
         "(-f epub -f pdf / -f epub,pdf). Each format is ONE file with every chapter.")]
AllOpt = Annotated[bool, typer.Option("--all", "-a", help="Download every chapter.")]
FirstOpt = Annotated[int | None, typer.Option("--first", help="Only the first N chapters.",
                                              min=1)]
LastOpt = Annotated[int | None, typer.Option("--last", help="Only the last N chapters.", min=1)]
RangeOpt = Annotated[str | None, typer.Option(
    "--range", "-r", help='Chapter numbers, e.g. "1-100", "5,9,20-", "-50".')]
VolumesOpt = Annotated[str | None, typer.Option("--volumes", help='Volume numbers, e.g. "1,3-4".')]
SourceOpt = Annotated[str | None, typer.Option(
    "--source", "-s", help="Force a crawler by name (see `novelbot sources`), e.g. generic.")]


def _settings(config: Path | None, verbose: int, quiet: bool, **overrides: object) -> Settings:
    settings = load_settings(config)
    ui.setup_logging(verbose, quiet, settings.log_file)
    h, d = settings.http, settings.download
    mapping = {
        "output": (d, "output_dir"), "concurrency": (d, "concurrency"), "delay": (h, "delay"),
        "retries": (h, "retries"), "timeout": (h, "timeout"),
    }
    for key, value in overrides.items():
        if value is None:
            continue
        if key in mapping:
            target, attr = mapping[key]
            setattr(target, attr, value)
        elif key == "ignore_robots" and value:
            h.respect_robots = False
        elif key == "no_images" and value:
            d.download_images = False
        elif key == "keep_author_notes" and value:
            d.keep_author_notes = True
    return settings


def _finish(code: int) -> None:
    if code:
        raise typer.Exit(code)


def _version(value: bool) -> None:
    if value:
        ui.console.print(f"novelbot {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", "-V", callback=_version, is_eager=True,
                                          help="Show the version and exit.")] = False,
) -> None:
    """Run without a command to start the interactive mode."""
    if ctx.invoked_subcommand is None:
        interactive(config=None, verbose=0, quiet=False)


# --------------------------------------------------------------------------
# crawl
# --------------------------------------------------------------------------
@app.command()
def crawl(
    url: Annotated[str, typer.Argument(help="Novel page URL (or a chapter URL).")],
    formats: FormatOpt = None,
    all_chapters: AllOpt = False,
    first: FirstOpt = None,
    last: LastOpt = None,
    chapter_range: RangeOpt = None,
    volumes: VolumesOpt = None,
    output: Annotated[Path | None, typer.Option("--output", "-o",
                                                help="Folder for novels (default: ./Novels).")] = None,
    concurrency: Annotated[int | None, typer.Option("--concurrency", "-j", min=1, max=32,
                                                    help="Parallel chapter downloads.")] = None,
    delay: Annotated[float | None, typer.Option("--delay", min=0,
                                                help="Min seconds between requests per site.")] = None,
    retries: Annotated[int | None, typer.Option("--retries", min=0)] = None,
    timeout: Annotated[float | None, typer.Option("--timeout", min=1)] = None,
    source: SourceOpt = None,
    force: Annotated[bool, typer.Option("--force", help="Re-download chapters already saved.")] = False,
    no_images: Annotated[bool, typer.Option("--no-images", help="Skip images in chapters.")] = False,
    keep_author_notes: Annotated[bool, typer.Option("--keep-author-notes")] = False,
    ignore_robots: Annotated[bool, typer.Option(
        "--ignore-robots", help="Do not check robots.txt. Only use on sites you have permission "
                                "to download from.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Never prompt; default to all chapters.")] = False,
    config: ConfigOpt = None,
    verbose: VerboseOpt = 0,
    quiet: QuietOpt = False,
) -> None:
    """Download a novel and save it as one EPUB / PDF / TXT / JSON file.

    Re-running the same command resumes: saved chapters are skipped.

    [bold]Examples[/]
      novelbot crawl https://www.royalroad.com/fiction/12345/x -f epub --all
      novelbot crawl URL -f epub,pdf --first 100
      novelbot crawl URL -f pdf -r 200-
    """
    settings = _settings(config, verbose, quiet, output=output, concurrency=concurrency,
                         delay=delay, retries=retries, timeout=timeout, ignore_robots=ignore_robots,
                         no_images=no_images, keep_author_notes=keep_author_notes)
    if ignore_robots:
        ui.err_console.print("[yellow]robots.txt checks are disabled for this run.[/]")
    request = runner.CrawlRequest(
        url=url,
        selection=Selection(all=all_chapters, first=first, last=last, ranges=chapter_range,
                            volumes=volumes),
        formats=runner.split_formats(formats) or None,
        source=source,
        force=force,
        prompt_selection=not yes,
    )
    _finish(runner.run(lambda: runner.crawl(settings, request)))


# --------------------------------------------------------------------------
# info
# --------------------------------------------------------------------------
@app.command()
def info(
    url: Annotated[str, typer.Argument(help="Novel page URL.")],
    chapters: Annotated[bool, typer.Option("--chapters", "-l", help="List every chapter.")] = False,
    source: SourceOpt = None,
    config: ConfigOpt = None,
    verbose: VerboseOpt = 0,
    quiet: QuietOpt = False,
) -> None:
    """Show a novel's details and chapter count without downloading chapters."""
    settings = _settings(config, verbose, quiet)

    async def flow() -> int:
        async with Engine(settings) as engine:
            with ui.err_console.status("Connecting...") as status:
                session = await engine.open(url, source=source, on_status=status.update,
                                            persist=False)
        ui.console.print(ui.novel_panel(session.novel))
        if chapters:
            table = Table(show_header=True, header_style="bold")
            table.add_column("#", justify="right")
            table.add_column("Title")
            table.add_column("Volume", style="dim")
            for ch in session.novel.chapters:
                table.add_row(str(ch.id), ch.title, ch.volume or "")
            ui.console.print(table)
        return runner.EXIT_OK

    _finish(runner.run(flow))


# --------------------------------------------------------------------------
# update / export
# --------------------------------------------------------------------------
FolderArg = Annotated[Path, typer.Argument(help="A downloaded novel folder (contains meta.json).",
                                           exists=True, file_okay=False)]


@app.command()
def update(
    folder: FolderArg,
    formats: FormatOpt = None,
    config: ConfigOpt = None,
    verbose: VerboseOpt = 0,
    quiet: QuietOpt = False,
) -> None:
    """Check a downloaded novel for new chapters, fetch them and rebuild the books."""
    settings = _settings(config, verbose, quiet, output=folder.resolve().parent)
    try:
        url = runner.novel_url_from_folder(folder)
    except NovelBotError as exc:
        ui.err_console.print(f"[bold red]Error:[/] {exc}")
        raise typer.Exit(runner.EXIT_ERROR) from exc
    request = runner.CrawlRequest(url=url, selection=Selection(all=True),
                                  formats=runner.split_formats(formats) or None,
                                  prompt_selection=False)
    _finish(runner.run(lambda: runner.crawl(settings, request)))


@app.command("export")
def export_cmd(
    folder: FolderArg,
    formats: FormatOpt = None,
    first: FirstOpt = None,
    last: LastOpt = None,
    chapter_range: RangeOpt = None,
    volumes: VolumesOpt = None,
    config: ConfigOpt = None,
    verbose: VerboseOpt = 0,
    quiet: QuietOpt = False,
) -> None:
    """Rebuild EPUB/PDF/TXT/JSON from already-downloaded chapters (works offline)."""
    settings = _settings(config, verbose, quiet)
    selection = Selection(first=first, last=last, ranges=chapter_range, volumes=volumes)
    _finish(runner.run(lambda: runner.export_local(settings, folder, selection,
                                                   runner.split_formats(formats) or None)))


# --------------------------------------------------------------------------
# sources / config
# --------------------------------------------------------------------------
@app.command()
def sources(config: ConfigOpt = None) -> None:
    """List the supported sites."""
    from novelbot.sources import load_sources

    settings = load_settings(config)
    table = Table(title="Sources", header_style="bold")
    table.add_column("Name", style="cyan")
    table.add_column("Site")
    table.add_column("Hosts", style="dim")
    for name, cls in sorted(load_sources(settings).items()):
        hosts = ", ".join(sorted(h for h in cls.hosts() if not h.startswith("www."))) or \
            "any site (fallback: heuristics or \\[sites] config)"
        table.add_row(name, cls.display_name or name, hosts)
    for name, site in sorted(settings.sites.items()):
        table.add_row(name, "config \\[sites]", ", ".join(site.hosts))
    ui.console.print(table)


@config_app.command("path")
def config_path() -> None:
    """Print where the config file is (or would be) loaded from."""
    found = find_config_file()
    ui.console.print(f"Active config : {found or '(none, using defaults)'}")
    ui.console.print(f"User config   : {user_config_file()}")


@config_app.command("show")
def config_show(config: ConfigOpt = None) -> None:
    """Print the effective settings."""
    from dataclasses import asdict

    from rich.pretty import Pretty

    from novelbot.core.pdf import describe_font

    settings = load_settings(config)
    data = asdict(settings)
    ui.console.print(f"Loaded from: {settings.source_path or '(defaults)'}")
    ui.console.print(Pretty(data, expand_all=True))
    ui.console.print(f"PDF font (auto): {describe_font()}")


@config_app.command("init")
def config_init(
    path: Annotated[Path | None, typer.Argument(help="Where to write (default: user config).")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a commented example config file."""
    from importlib.resources import files

    target = path or user_config_file()
    if target.exists() and not force:
        ui.err_console.print(f"[yellow]{target} already exists[/] (use --force to overwrite)")
        raise typer.Exit(runner.EXIT_ERROR)
    template = files("novelbot").joinpath("example_config.toml").read_text("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(template, "utf-8")
    ui.console.print(f"Wrote {target}")


# --------------------------------------------------------------------------
# interactive
# --------------------------------------------------------------------------
@app.command()
def interactive(config: ConfigOpt = None, verbose: VerboseOpt = 0, quiet: QuietOpt = False) -> None:
    """Guided mode: paste a link, pick chapters and formats, download."""
    from rich.prompt import Confirm, Prompt

    settings = _settings(config, verbose, quiet)
    ui.console.rule(f"[bold cyan]novelbot {__version__}[/]")
    ui.console.print("Paste the link of a novel page (or of its first chapter). "
                     "Leave empty to quit.\n")
    code = runner.EXIT_OK
    while True:
        url = Prompt.ask("[bold]Novel URL[/]", default="", show_default=False,
                         console=ui.console).strip()
        if not url:
            break
        formats = runner.ask_formats(settings.download.formats)
        folder = Prompt.ask("Save into folder", default=str(settings.download.output_dir),
                            console=ui.console)
        settings.download.output_dir = Path(folder).expanduser()
        request = runner.CrawlRequest(url=url, formats=formats, prompt_selection=True)
        code = runner.run(lambda request=request: runner.crawl(settings, request))
        if not Confirm.ask("\nDownload another novel?", default=False, console=ui.console):
            break
    _finish(code)


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover - interactive prompt abort
        ui.err_console.print("\n[yellow]Bye.[/]")
        raise SystemExit(runner.EXIT_INTERRUPTED) from None


__all__ = ["app", "main"]
