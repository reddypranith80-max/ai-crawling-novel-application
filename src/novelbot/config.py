"""Configuration: defaults, TOML loading and CLI overrides.

Lookup order for the config file (first hit wins):

1. ``--config PATH`` on the command line
2. ``$NOVELBOT_CONFIG``
3. ``./novelbot.toml`` in the current directory
4. the per-user config file (``novelbot config path`` prints it)

Every key is optional; see ``novelbot.example.toml`` for the full list.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, get_type_hints

from platformdirs import user_config_path

from novelbot import __version__
from novelbot.errors import ConfigError

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = f"Mozilla/5.0 (compatible; novelbot/{__version__}; personal offline reader)"
#: Token matched against ``User-agent:`` lines in robots.txt.
ROBOTS_TOKEN = "novelbot"
SUPPORTED_FORMATS = ("epub", "pdf", "txt", "json")


@dataclass
class HttpSettings:
    user_agent: str = DEFAULT_USER_AGENT
    timeout: float = 30.0
    retries: int = 4
    #: Base seconds for exponential backoff between retries.
    backoff: float = 2.0
    #: Minimum seconds between two requests to the same host.
    delay: float = 1.5
    #: Random extra delay (0..jitter seconds) added to every request.
    jitter: float = 0.5
    respect_robots: bool = True
    verify_ssl: bool = True
    proxy: str | None = None
    #: Extra headers sent with every request.
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class DownloadSettings:
    output_dir: Path = Path("Novels")
    #: Parallel chapter downloads. The per-host delay still applies.
    concurrency: int = 3
    formats: list[str] = field(default_factory=lambda: ["epub"])
    download_images: bool = True
    keep_author_notes: bool = False


@dataclass
class EpubSettings:
    include_cover: bool = True
    include_info_page: bool = True
    #: When a novel has no volumes, group the table of contents into blocks
    #: of this many chapters (keeps 1000+ chapter TOCs navigable). 0 = flat.
    toc_group_size: int = 100
    #: Optional path to a CSS file that replaces the built-in stylesheet.
    stylesheet: Path | None = None


@dataclass
class PdfSettings:
    page_size: str = "A5"
    font_size: float = 11.0
    #: Path to a regular .ttf font. Empty = auto-detect a Unicode system font.
    font: Path | None = None
    font_bold: Path | None = None
    font_italic: Path | None = None
    font_bold_italic: Path | None = None
    #: Extra fonts used for glyphs the main font lacks (e.g. a CJK font).
    fallback_fonts: list[Path] = field(default_factory=list)
    include_images: bool = True


@dataclass
class SiteConfig:
    """Selectors for the config-driven generic crawler (``[sites.<name>]``)."""

    name: str = ""
    hosts: list[str] = field(default_factory=list)
    title: str | None = None
    author: str | None = None
    cover: str | None = None
    synopsis: str | None = None
    tags: str | None = None
    #: Selector matching chapter ``<a>`` links on the table-of-contents page.
    chapter_list: str | None = None
    #: Selector of the "next page" link when the TOC itself is paginated.
    toc_next: str | None = None
    #: Set when the TOC lists newest chapters first.
    reverse_chapters: bool = False
    #: Selector of the chapter body.
    content: str | None = None
    chapter_title: str | None = None
    #: Selector of the "next chapter" link. Used when there is no TOC: the
    #: crawler starts at the given chapter URL and follows these links.
    next_chapter: str | None = None
    remove: list[str] = field(default_factory=list)
    bad_text: list[str] = field(default_factory=list)
    language: str = "en"
    delay: float | None = None


@dataclass
class Settings:
    http: HttpSettings = field(default_factory=HttpSettings)
    download: DownloadSettings = field(default_factory=DownloadSettings)
    epub: EpubSettings = field(default_factory=EpubSettings)
    pdf: PdfSettings = field(default_factory=PdfSettings)
    sites: dict[str, SiteConfig] = field(default_factory=dict)
    #: Extra directories scanned for ``*.py`` crawler plugins.
    plugin_dirs: list[Path] = field(default_factory=list)
    log_file: Path | None = None
    #: Where the settings were loaded from (None = defaults only).
    source_path: Path | None = None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def user_config_file() -> Path:
    return user_config_path("novelbot", appauthor=False) / "config.toml"


def find_config_file(explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        if not explicit.is_file():
            raise ConfigError(f"Config file not found: {explicit}")
        return explicit
    candidates: list[Path] = []
    if env := os.environ.get("NOVELBOT_CONFIG"):
        candidates.append(Path(env))
    candidates += [Path.cwd() / "novelbot.toml", user_config_file()]
    return next((p for p in candidates if p.is_file()), None)


def load_settings(path: Path | None = None) -> Settings:
    """Load settings from ``path`` (or the default search order)."""
    found = find_config_file(path)
    settings = Settings()
    if found is None:
        return settings
    try:
        with found.open("rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read config {found}: {exc}") from exc

    base_dir = found.parent
    for section in ("http", "download", "epub", "pdf"):
        if section in raw:
            _apply(getattr(settings, section), raw.pop(section), f"[{section}]", base_dir)
    for name, site_raw in (raw.pop("sites", None) or {}).items():
        site = SiteConfig(name=name)
        _apply(site, site_raw, f"[sites.{name}]", base_dir)
        if not site.hosts:
            raise ConfigError(f"[sites.{name}] needs a 'hosts' list")
        settings.sites[name] = site
    if "plugin_dirs" in raw:
        settings.plugin_dirs = [_resolve(Path(p), base_dir) for p in raw.pop("plugin_dirs")]
    if "log_file" in raw:
        settings.log_file = _resolve(Path(raw.pop("log_file")), base_dir)
    for key in raw:
        log.warning("Unknown config key %r in %s (ignored)", key, found)
    settings.source_path = found
    return settings


def _apply(target: Any, values: dict[str, Any], where: str, base_dir: Path) -> None:
    """Copy ``values`` onto dataclass ``target`` with light type coercion."""
    if not isinstance(values, dict):
        raise ConfigError(f"{where} must be a table")
    hints = get_type_hints(type(target))
    names = {f.name for f in fields(target)}
    for key, value in values.items():
        attr = key.replace("-", "_")
        if attr not in names or attr in ("name", "source_path"):
            log.warning("Unknown config key %r in %s (ignored)", key, where)
            continue
        setattr(target, attr, _coerce(value, hints[attr], f"{where} {key}", base_dir))


def _coerce(value: Any, hint: Any, where: str, base_dir: Path) -> Any:
    text = str(hint)
    if value is None:
        return None
    if "Path" in text:
        if isinstance(value, list):
            return [_resolve(Path(v), base_dir) for v in value]
        return _resolve(Path(value), base_dir) if value else None
    if hint is bool or text == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: expected true/false, got {value!r}")
        return value
    if hint is int or text == "int":
        return _number(int, value, where)
    if hint is float or text.startswith("float"):
        return _number(float, value, where)
    if text.startswith("list") and isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if is_dataclass(hint):
        raise ConfigError(f"{where}: nested tables are not supported here")
    return value


def _number(kind: type, value: Any, where: str) -> Any:
    try:
        return kind(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where}: expected a number, got {value!r}") from exc


def _resolve(path: Path, base_dir: Path) -> Path:
    path = path.expanduser()
    return path if path.is_absolute() else (base_dir / path)


def validate(settings: Settings) -> None:
    """Sanity-check values after CLI overrides have been applied."""
    h, d = settings.http, settings.download
    if h.delay < 0 or h.jitter < 0 or h.timeout <= 0:
        raise ConfigError("delay/jitter must be >= 0 and timeout > 0")
    if h.retries < 0:
        raise ConfigError("retries must be >= 0")
    if not 1 <= d.concurrency <= 32:
        raise ConfigError("concurrency must be between 1 and 32")
    bad = [f for f in d.formats if f not in SUPPORTED_FORMATS]
    if bad:
        raise ConfigError(
            f"Unknown output format(s): {', '.join(bad)}. Choose from {', '.join(SUPPORTED_FORMATS)}"
        )
