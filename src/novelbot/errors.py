"""Exception hierarchy. Everything the CLI reports nicely derives from NovelBotError."""

from __future__ import annotations


class NovelBotError(Exception):
    """Base class for all expected, user-facing errors."""


class ConfigError(NovelBotError):
    """The configuration file is invalid."""


class SourceNotFoundError(NovelBotError):
    """No crawler can handle the given URL."""


class RobotsDisallowedError(NovelBotError):
    """robots.txt forbids fetching the URL."""

    def __init__(self, url: str) -> None:
        super().__init__(
            f"robots.txt disallows fetching {url}. "
            "Respecting the site's rules; nothing was downloaded from this path."
        )
        self.url = url


class HttpError(NovelBotError):
    """A request failed permanently (after retries)."""

    def __init__(self, message: str, *, url: str, status: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status = status


class BlockedError(HttpError):
    """The site refused automated access (Cloudflare challenge, HTTP 418, ...).

    These are never retried: hammering a site that is actively refusing us is
    exactly what we must not do.
    """


class ParseError(NovelBotError):
    """A page did not have the structure the crawler expected."""

    def __init__(self, message: str, *, url: str | None = None) -> None:
        hint = " The site's layout may have changed."
        super().__init__(message + (f" ({url})" if url else "") + hint)
        self.url = url


class AccessDeniedError(NovelBotError):
    """The content exists but needs a login or is otherwise restricted."""


class EmptyChapterError(NovelBotError):
    """A chapter page was fetched but contained no readable text."""
