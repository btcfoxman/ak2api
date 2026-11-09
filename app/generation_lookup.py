"""Bounded, resumable reads of an accepted generation's existing result list.

This module makes no requests. A lookup miss is visibility information, never
evidence that a paid generation failed or may be submitted again.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import math
import threading
import time
from typing import Any, Callable


def _owner(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not value.isascii() or not value.isdigit() or len(value) > 19:
            return None
        value = int(value)
    return value if isinstance(value, int) and 0 < value < 2**63 else None


def _resource(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError("an accepted resource ID is required for result lookup")
    return value.strip()


def _cursor(value: Any) -> str | None:
    if value is None or value == "" or value == 0:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError("invalid result-list cursor")
    return value


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _State:
    next_cursor: str | None = None
    deep_pages: int = 0
    seen: frozenset[str] = frozenset()


@dataclass
class _Entry:
    state: _State
    version: int
    touched: float


class GenerationLookupCache:
    """LRU snapshots with versioned writes; private caches belong to one client."""

    def __init__(self, *, max_entries: int = 256, ttl_seconds: float = 3600,
                 max_deep_pages: int = 256, clock: Callable[[], float] = time.monotonic):
        if type(max_entries) is not int or not 1 <= max_entries <= 256:
            raise ValueError("lookup cache capacity must be between 1 and 256")
        if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float))
                or not math.isfinite(ttl_seconds) or not 0 < ttl_seconds <= 3600):
            raise ValueError("lookup cache TTL must be positive and at most 3600 seconds")
        if type(max_deep_pages) is not int or not 1 <= max_deep_pages <= 256:
            raise ValueError("lookup history limit must be between 1 and 256 pages")
        self.max_entries = max_entries
        self.ttl_seconds = float(ttl_seconds)
        self.max_deep_pages = max_deep_pages
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: OrderedDict[tuple[Any, str], _Entry] = OrderedDict()
        self._version = 0
        self._private_owner = object()

    def _now(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value):
            raise ValueError("invalid lookup clock")
        return value

    def _prune(self, now: float) -> None:
        for key, entry in list(self._entries.items()):
            if now - entry.touched >= self.ttl_seconds:
                del self._entries[key]

    @property
    def entry_count(self) -> int:
        with self._lock:
            self._prune(self._now())
            return len(self._entries)

    def begin(self, owner_id: Any, resource_id: str) -> GenerationLookupRound:
        """Known owners can share this cache; unknown owners get no shared state."""
        owner_id = _owner(owner_id)
        if owner_id is None:
            return GenerationLookupCache(max_entries=1, ttl_seconds=self.ttl_seconds,
                                         max_deep_pages=self.max_deep_pages,
                                         clock=self._clock)._begin_private(resource_id)
        return self._begin((owner_id, _resource(resource_id)))

    def _begin_private(self, resource_id: str) -> GenerationLookupRound:
        return self._begin((self._private_owner, _resource(resource_id)))

    def _begin(self, key: tuple[Any, str]) -> GenerationLookupRound:
        with self._lock:
            now = self._now()
            self._prune(now)
            entry = self._entries.get(key)
            if entry is None:
                self._version += 1
                entry = _Entry(_State(), self._version, now)
                self._entries[key] = entry
            entry.touched = now
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            # The snapshot is immutable; the round makes its own mutable seen set.
            return GenerationLookupRound(self, key, entry.version, entry.state)

    def _save(self, key: tuple[Any, str], version: int, state: _State | None) -> bool:
        with self._lock:
            now = self._now()
            self._prune(now)
            current = self._entries.get(key)
            if current is None or current.version != version:
                return False
            if state is None:
                del self._entries[key]
            else:
                self._version += 1
                self._entries[key] = _Entry(state, self._version, now)
                self._entries.move_to_end(key)
            return True

    def _found(self, key: tuple[Any, str]) -> None:
        # Finding this exact accepted resource invalidates any concurrent scan.
        # Missing entries cannot be recreated by stale CAS writers.
        with self._lock:
            self._entries.pop(key, None)


_NO_PAGE = object()


class GenerationLookupRound:
    """Read the recent page first, then at most four older pages per round."""

    def __init__(self, cache: GenerationLookupCache, key: tuple[Any, str],
                 version: int, state: _State):
        self._cache, self._key, self._version = cache, key, version
        self._next = state.next_cursor
        self._deep_pages = state.deep_pages
        self._seen = set(state.seen)
        self._pending: object | str = _NO_PAGE
        self._requests = self._observed = 0
        self._resumed = False
        self._dirty = self._reset = self._done = False
        self._status = "not_started"
        self._result: dict[str, Any] | None = None

    def next_cursor(self) -> str | None:
        if self._done or self._result is not None:
            return None
        if self._pending is not _NO_PAGE:
            raise RuntimeError("observe the previous lookup page before requesting another")
        if self._requests >= 5:
            return None
        cursor = "" if self._requests == 0 else self._next
        if cursor is None:
            return None
        if cursor and _fingerprint(cursor) in self._seen:
            self._boundary("cursor_cycle")
            return None
        if cursor and self._deep_pages >= self._cache.max_deep_pages:
            self._boundary("scan_capped")
            return None
        self._pending = cursor
        self._requests += 1
        if cursor and self._requests == 2 and self._deep_pages:
            self._resumed = True
        return cursor

    def _boundary(self, status: str) -> None:
        self._next = None
        self._status, self._dirty, self._reset, self._done = status, True, True, True

    def observe(self, next_cursor: Any) -> None:
        if self._done or self._result is not None or self._pending is _NO_PAGE:
            raise RuntimeError("no pending lookup page to observe")
        requested = self._pending
        self._pending = _NO_PAGE
        self._observed += 1
        self._dirty = True
        if requested:
            self._deep_pages += 1
            self._seen.add(_fingerprint(str(requested)))
        try:
            following = _cursor(next_cursor)
        except ValueError:
            self._boundary("invalid_cursor")
            return
        if following is None:
            self._boundary("not_found")
            return
        # A fresh first page is always checked, but it must not restart an older
        # traversal already in progress for the same owner and accepted resource.
        if not requested and self._next:
            following = self._next
        if _fingerprint(following) in self._seen:
            self._boundary("cursor_cycle")
        elif self._deep_pages >= self._cache.max_deep_pages:
            self._boundary("scan_capped")
        else:
            self._next, self._status = following, "scanning"

    def found(self) -> dict[str, Any]:
        if self._result is not None:
            return dict(self._result)
        if self._pending is _NO_PAGE:
            raise RuntimeError("a matching resource requires a pending lookup page")
        self._observed += 1
        if self._pending:
            self._deep_pages += 1
        self._pending = _NO_PAGE
        self._status, self._done = "found", True
        self._next = None
        self._cache._found(self._key)
        self._result = self._diagnostic(state_saved=True, concurrent_update=False)
        return dict(self._result)

    def _diagnostic(self, *, state_saved: bool, concurrent_update: bool) -> dict[str, Any]:
        return {"status": self._status, "pages_requested": self._requests,
                "pages_observed": self._observed, "deep_pages_scanned": self._deep_pages,
                "resumed": self._resumed, "has_more": bool(self._next) and not self._reset,
                "state_saved": state_saved, "concurrent_update": concurrent_update}

    def finish(self) -> dict[str, Any]:
        """Commit completed reads, including when a subsequent read raised."""
        if self._result is not None:
            return dict(self._result)
        if self._pending is not _NO_PAGE:
            self._status = "query_interrupted"
        saved = False
        if self._dirty:
            state = None if self._reset else _State(self._next, self._deep_pages, frozenset(self._seen))
            saved = self._cache._save(self._key, self._version, state)
        self._result = self._diagnostic(state_saved=saved,
                                       concurrent_update=self._dirty and not saved)
        self._done = True
        return dict(self._result)


_SHARED_CACHE = GenerationLookupCache()


def begin_lookup(owner_id: Any, resource_id: str, *,
                 private_cache: GenerationLookupCache | None = None) -> GenerationLookupRound:
    """Share known account scans; unknown owners need a client-owned private cache."""
    if _owner(owner_id) is not None:
        return _SHARED_CACHE.begin(owner_id, resource_id)
    cache = private_cache if private_cache is not None and private_cache is not _SHARED_CACHE else GenerationLookupCache()
    return cache._begin_private(resource_id)
