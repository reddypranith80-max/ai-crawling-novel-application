"""Per-host request pacing.

Every request to a host waits until at least ``delay`` (+ random jitter)
seconds have passed since the previous request to that host started. Hosts
are independent, so a cover image CDN does not slow down chapter pages.
"""

from __future__ import annotations

import asyncio
import random
from collections import defaultdict


class HostRateLimiter:
    def __init__(self, delay: float, jitter: float = 0.0, *, max_penalty: float = 8.0) -> None:
        self.delay = max(0.0, delay)
        self.jitter = max(0.0, jitter)
        self.max_penalty = max_penalty
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._next_slot: dict[str, float] = {}
        self._floor: dict[str, float] = {}
        self._penalty: dict[str, float] = {}

    def set_min_delay(self, host: str, seconds: float) -> None:
        """Raise the delay for one host (robots.txt Crawl-delay, crawler minimums)."""
        self._floor[host] = max(self._floor.get(host, 0.0), seconds)

    def interval(self, host: str) -> float:
        base = max(self.delay, self._floor.get(host, 0.0))
        return base * self._penalty.get(host, 1.0)

    def penalize(self, host: str) -> None:
        """Slow down a host that answered 429/503 (doubles, capped)."""
        self._penalty[host] = min(self.max_penalty, self._penalty.get(host, 1.0) * 2)

    def relax(self, host: str) -> None:
        """Gradually undo penalties after successful requests."""
        if host in self._penalty:
            value = self._penalty[host] * 0.9
            if value <= 1.0:
                del self._penalty[host]
            else:
                self._penalty[host] = value

    async def wait(self, host: str) -> None:
        async with self._locks[host]:
            loop = asyncio.get_running_loop()
            delay = self._next_slot.get(host, 0.0) - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            extra = random.uniform(0, self.jitter) if self.jitter else 0.0
            self._next_slot[host] = loop.time() + self.interval(host) + extra
