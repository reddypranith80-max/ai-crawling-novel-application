"""novelbot: download web novels from their public web pages to your own device.

The package is organised as:

* :mod:`novelbot.core`    -- HTTP client, robots.txt, rate limiting, engine,
  downloader, storage, content cleaning and the EPUB/TXT/JSON exporters.
* :mod:`novelbot.sources` -- one crawler per site, all subclassing
  :class:`novelbot.sources.base.BaseCrawler`.
* :mod:`novelbot.cli`     -- the Typer command-line interface.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
