"""Built-in sources, plugin loading and URL -> crawler resolution."""

from __future__ import annotations

import importlib
import importlib.util
import logging
import pkgutil
import sys
from importlib.metadata import entry_points
from pathlib import Path

from novelbot.config import Settings
from novelbot.core.http import HttpClient, host_of
from novelbot.errors import SourceNotFoundError
from novelbot.sources.base import BaseCrawler, registered_crawlers

log = logging.getLogger(__name__)

_loaded = False


def load_sources(settings: Settings | None = None) -> dict[str, type[BaseCrawler]]:
    """Import built-in sources, entry-point plugins and plugin directories."""
    global _loaded
    if not _loaded:
        package_dir = Path(__file__).parent
        for module in pkgutil.iter_modules([str(package_dir)]):
            if module.name != "base":
                importlib.import_module(f"{__name__}.{module.name}")
        for ep in entry_points(group="novelbot.sources"):
            try:
                ep.load()
            except Exception as exc:  # a broken plugin must not break the tool
                log.warning("Could not load source plugin %s: %s", ep.name, exc)
        _loaded = True
    if settings is not None:
        for directory in settings.plugin_dirs:
            _load_plugin_dir(directory)
    return registered_crawlers()


def _load_plugin_dir(directory: Path) -> None:
    if not directory.is_dir():
        log.warning("Plugin directory %s does not exist", directory)
        return
    for path in sorted(directory.glob("*.py")):
        module_name = f"novelbot_plugin_{path.stem}"
        if module_name in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            del sys.modules[module_name]
            log.warning("Could not load plugin %s: %s", path, exc)


def create_crawler(
    url: str,
    http: HttpClient,
    settings: Settings,
    *,
    source: str | None = None,
) -> BaseCrawler:
    """Pick the crawler for ``url``.

    Order: explicit ``source`` name, a dedicated crawler for the host, a
    ``[sites.*]`` entry from the config, then the heuristic generic crawler.
    """
    from novelbot.sources.generic import GenericCrawler

    crawlers = load_sources(settings)
    host = host_of(url).removeprefix("www.")

    if source:
        if source in settings.sites:
            return GenericCrawler(http, settings, site=settings.sites[source])
        if source not in crawlers:
            names = ", ".join(sorted([*crawlers, *settings.sites]))
            raise SourceNotFoundError(f"Unknown source {source!r}. Available: {names}")
        cls = crawlers[source]
        if cls is GenericCrawler:
            return GenericCrawler(http, settings, site=_site_for_host(settings, host))
        return cls(http, settings)

    for cls in crawlers.values():
        if cls is not GenericCrawler and cls.can_handle(url):
            return cls(http, settings)

    site = _site_for_host(settings, host)
    if site is None:
        log.warning("No dedicated source for %s; using generic page heuristics. "
                    "For reliable results add a [sites] entry to your config.", host)
    return GenericCrawler(http, settings, site=site)


def _site_for_host(settings: Settings, host: str):
    for site in settings.sites.values():
        if host in {h.lower().removeprefix("www.") for h in site.hosts}:
            return site
    return None


__all__ = ["BaseCrawler", "create_crawler", "load_sources"]
