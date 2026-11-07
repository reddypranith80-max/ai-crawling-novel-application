"""Async HTTP client with politeness built in.

Every request goes through :meth:`HttpClient.request`, which

1. checks robots.txt for the target host (cached per origin),
2. waits for the per-host rate limiter,
3. retries transient failures (network errors, 429, 5xx) with exponential
   backoff, honouring ``Retry-After``,
4. detects block pages (Cloudflare challenges, HTTP 418) and raises
   :class:`BlockedError` immediately instead of retrying.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from novelbot.config import ROBOTS_TOKEN, HttpSettings
from novelbot.core.ratelimit import HostRateLimiter
from novelbot.core.robots import RobotsTxt
from novelbot.errors import BlockedError, HttpError, RobotsDisallowedError

log = logging.getLogger(__name__)

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
MAX_BACKOFF = 90.0
MAX_CRAWL_DELAY = 60.0


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _looks_blocked(response: httpx.Response) -> str | None:
    """Return a reason string if the response is a bot-block page."""
    if response.headers.get("cf-mitigated") == "challenge":
        return "Cloudflare challenge"
    if response.status_code == 418:
        return "HTTP 418 (the site refuses automated clients)"
    if response.status_code in (403, 429, 503):
        head = response.text[:4000] if "html" in response.headers.get("content-type", "") else ""
        if "_cf_chl_opt" in head or "<title>Just a moment...</title>" in head:
            return "Cloudflare challenge"
        if "captcha" in head.lower() and response.status_code == 403:
            return "CAPTCHA page"
    return None


class HttpClient:
    """Shared async HTTP client. Use as an async context manager."""

    def __init__(
        self,
        settings: HttpSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_connections: int = 10,
    ) -> None:
        self.settings = settings
        self.limiter = HostRateLimiter(settings.delay, settings.jitter)
        headers = {
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
            **settings.headers,
        }
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(settings.timeout),
            follow_redirects=True,
            verify=settings.verify_ssl,
            proxy=settings.proxy,
            transport=transport,
            limits=httpx.Limits(max_connections=max_connections),
        )
        self._robots: dict[str, RobotsTxt] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        #: Hosts that already served a block page; further requests fail fast.
        self._blocked: dict[str, str] = {}

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def cookies(self) -> httpx.Cookies:
        return self._client.cookies

    # ------------------------------------------------------------------ robots
    async def robots_for(self, url: str) -> RobotsTxt:
        origin = origin_of(url)
        if origin in self._robots:
            return self._robots[origin]
        lock = self._robots_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin not in self._robots:
                self._robots[origin] = await self._fetch_robots(origin)
                delay = self._robots[origin].crawl_delay(ROBOTS_TOKEN)
                if delay:
                    capped = min(delay, MAX_CRAWL_DELAY)
                    log.info("%s asks for Crawl-delay %.1fs", origin, capped)
                    self.limiter.set_min_delay(host_of(origin), capped)
        return self._robots[origin]

    async def _fetch_robots(self, origin: str) -> RobotsTxt:
        url = origin + "/robots.txt"
        try:
            response = await self._send("GET", url, check_robots=False, raise_status=False)
        except BlockedError:
            raise
        except HttpError as exc:
            # RFC 9309: an unreachable robots.txt means "assume disallowed".
            log.warning("Could not fetch %s (%s); treating site as disallowed", url, exc)
            return RobotsTxt.disallowing_all()
        if response.status_code >= 500:
            return RobotsTxt.disallowing_all()
        if response.status_code >= 400:
            return RobotsTxt.allowing_all()  # RFC 9309: 4xx = no restrictions
        return RobotsTxt.parse(response.text)

    async def ensure_allowed(self, url: str) -> None:
        if not self.settings.respect_robots:
            return
        robots = await self.robots_for(url)
        if not robots.can_fetch(ROBOTS_TOKEN, url):
            raise RobotsDisallowedError(url)

    # ----------------------------------------------------------------- request
    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Send a request with robots, rate limiting, retries and block detection."""
        return await self._send(method, url, check_robots=True, **kwargs)

    async def _send(
        self,
        method: str,
        url: str,
        *,
        check_robots: bool,
        raise_status: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        host = host_of(url)
        if not host:
            raise HttpError(f"Invalid URL: {url}", url=url)
        if host in self._blocked:
            raise BlockedError(self._blocked[host], url=url)
        if check_robots:
            await self.ensure_allowed(url)

        attempts = self.settings.retries + 1
        last_error: str = "unknown error"
        for attempt in range(1, attempts + 1):
            await self.limiter.wait(host)
            try:
                response = await self._client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                log.debug("%s %s failed (%s), attempt %d/%d", method, url, last_error,
                          attempt, attempts)
                if attempt < attempts:
                    await asyncio.sleep(self._backoff(attempt))
                continue

            if reason := _looks_blocked(response):
                message = (
                    f"{host} blocked automated access ({reason}). novelbot does not try to "
                    "bypass bot protection; try again later or from another network."
                )
                self._blocked[host] = message
                raise BlockedError(message, url=url, status=response.status_code)

            if response.status_code in RETRY_STATUSES and attempt < attempts:
                self.limiter.penalize(host)
                wait = self._retry_after(response) or self._backoff(attempt)
                log.warning("%s returned HTTP %d; retrying in %.0fs (attempt %d/%d)",
                            host, response.status_code, wait, attempt, attempts)
                await asyncio.sleep(wait)
                last_error = f"HTTP {response.status_code}"
                continue

            if raise_status and response.status_code >= 400:
                raise HttpError(f"HTTP {response.status_code} for {url}", url=url,
                                status=response.status_code)
            self.limiter.relax(host)
            return response

        raise HttpError(f"Giving up on {url} after {attempts} attempts: {last_error}", url=url)

    def _backoff(self, attempt: int) -> float:
        base = self.settings.backoff * (2 ** (attempt - 1))
        return min(MAX_BACKOFF, base + random.uniform(0, self.settings.backoff))

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("retry-after")
        if not value:
            return None
        try:
            seconds = float(value)
        except ValueError:
            try:
                parsed = email.utils.parsedate_to_datetime(value)
            except (TypeError, ValueError):
                return None
            if parsed is None or parsed.tzinfo is None:
                return None
            seconds = parsed.timestamp() - time.time()
        return max(0.0, min(seconds, MAX_BACKOFF * 2))

    # --------------------------------------------------------------- shortcuts
    async def get_text(self, url: str, **kwargs: Any) -> str:
        response = await self.request("GET", url, **kwargs)
        return response.text

    async def get_bytes(self, url: str, **kwargs: Any) -> tuple[bytes, str]:
        response = await self.request("GET", url, **kwargs)
        return response.content, response.headers.get("content-type", "")

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        response = await self.request("GET", url, **kwargs)
        return response.json()
