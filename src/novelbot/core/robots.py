"""robots.txt parsing and matching per RFC 9309.

Python's ``urllib.robotparser`` uses first-match semantics and ignores ``*``
and ``$`` wildcards, which gets common real-world files wrong (for example
``Disallow: /wp-admin/`` followed by ``Allow: /wp-admin/admin-ajax.php``).
This module implements the RFC rules instead:

* groups are selected by case-insensitive product-token match, falling back
  to ``*``; multiple matching groups are merged;
* the *longest* matching rule wins, and ``Allow`` wins a tie;
* ``*`` matches any sequence of characters and a trailing ``$`` anchors.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True, slots=True)
class Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]

    @classmethod
    def build(cls, allow: bool, pattern: str) -> Rule:
        pattern = unquote(pattern)
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        regex = ".*".join(re.escape(part) for part in body.split("*"))
        return cls(allow, pattern, re.compile(regex + ("$" if anchored else ""), re.DOTALL))

    def matches(self, path: str) -> bool:
        return self.regex.match(path) is not None


@dataclass(slots=True)
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    crawl_delay: float | None = None


class RobotsTxt:
    """A parsed robots.txt file."""

    def __init__(self, groups: list[_Group] | None = None, *, allow_all: bool = False,
                 disallow_all: bool = False) -> None:
        self._groups = groups or []
        self._allow_all = allow_all
        self._disallow_all = disallow_all

    # -- constructors --------------------------------------------------------
    @classmethod
    def allowing_all(cls) -> RobotsTxt:
        return cls(allow_all=True)

    @classmethod
    def disallowing_all(cls) -> RobotsTxt:
        return cls(disallow_all=True)

    @classmethod
    def parse(cls, text: str) -> RobotsTxt:
        groups: list[_Group] = []
        current: _Group | None = None
        last_was_agent = False
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key in ("user-agent", "useragent"):
                if current is None or not last_was_agent:
                    current = _Group()
                    groups.append(current)
                current.agents.append(value.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            if current is None:
                continue  # rules before any user-agent line are ignored
            if key in ("allow", "disallow"):
                if value:  # an empty Disallow means "allow everything"
                    current.rules.append(Rule.build(key == "allow", value))
            elif key == "crawl-delay":
                with contextlib.suppress(ValueError):
                    current.crawl_delay = float(value)
        return cls(groups)

    # -- queries ---------------------------------------------------------------
    def _select(self, token: str) -> list[_Group]:
        token = token.lower()
        specific = [g for g in self._groups if token in g.agents]
        if specific:
            return specific
        return [g for g in self._groups if "*" in g.agents]

    def can_fetch(self, token: str, url: str) -> bool:
        if self._allow_all:
            return True
        if self._disallow_all:
            return False
        parts = urlsplit(url)
        path = unquote(parts.path or "/")
        if parts.query:
            path += "?" + unquote(parts.query)
        if path == "/robots.txt":
            return True
        best: Rule | None = None
        for group in self._select(token):
            for rule in group.rules:
                if not rule.matches(path):
                    continue
                if (
                    best is None
                    or len(rule.pattern) > len(best.pattern)
                    or (len(rule.pattern) == len(best.pattern) and rule.allow)
                ):
                    best = rule
        return best is None or best.allow

    def crawl_delay(self, token: str) -> float | None:
        delays = [g.crawl_delay for g in self._select(token) if g.crawl_delay is not None]
        return max(delays) if delays else None
