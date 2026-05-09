"""In-process TTL cache for the slow / static Supabase lookups that the
analytics endpoints rely on.

Why
---
The analytics module makes 20+ sequential blocking HTTP round-trips to
Supabase per request — for tables that almost never change in real
time (``faculties``, ``departments``, ``courses``, ``student_profiles``).
At ~150–500 ms each from a Vietnam laptop to Supabase Singapore, a
single ``/analytics/overview`` call can spend tens of seconds just on
network latency, blocking the asyncio event loop for everything else.

This module gives those lookups a tiny **time-to-live cache** — the
*data* is identical to what Supabase returns, just memoised for a
short window so repeat calls don't pay the round-trip cost again.

Design notes
------------
* The cache is process-local (a plain ``dict``) — fine for a single
  uvicorn worker; if you scale to multiple workers each will have its
  own copy, which is acceptable because the cached data is small.
* TTLs are short on purpose (5 min for static reference tables, 60 s
  for high-write tables like ``course_enrollments``) so admin edits
  show up quickly without forcing a server restart.
* Eviction is opportunistic: when ``get`` finds an expired entry it
  drops it. We also expose a ``clear()`` for tests.
* Thread-safe enough — Python dict ops are atomic for our access
  pattern, and the lookups are idempotent so a thundering-herd race
  just refills the same value.
"""

from __future__ import annotations

import time
from threading import Lock
from typing import Any

# Sentinel signalling "no cached entry yet". Distinct from a cached
# ``None`` value (which means "Supabase returned nothing").
_MISS = object()


class _TTLEntry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: Any, expires_at: float) -> None:
        self.value = value
        self.expires_at = expires_at


class TTLCache:
    """Tiny TTL key/value cache with explicit invalidation."""

    def __init__(self, default_ttl_sec: float = 300.0) -> None:
        self._default_ttl = default_ttl_sec
        self._store: dict[str, _TTLEntry] = {}
        self._lock = Lock()

    def get(self, key: str) -> Any:
        """Return the cached value, or ``_MISS`` when absent / expired."""
        entry = self._store.get(key)
        if entry is None:
            return _MISS
        if entry.expires_at < time.monotonic():
            # Expired — drop it so the next call refreshes. Keep the
            # delete inside the lock so concurrent expiry doesn't bomb.
            with self._lock:
                self._store.pop(key, None)
            return _MISS
        return entry.value

    def set(self, key: str, value: Any, ttl_sec: float | None = None) -> None:
        ttl = ttl_sec if ttl_sec is not None else self._default_ttl
        expires_at = time.monotonic() + ttl
        with self._lock:
            self._store[key] = _TTLEntry(value, expires_at)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


# Two caches with different TTLs so we can be aggressive on the
# things that truly never change and conservative on the things that
# do. Tweak from the call site if you need finer control.
STATIC_CACHE = TTLCache(default_ttl_sec=300.0)   # faculties, departments, courses
DYNAMIC_CACHE = TTLCache(default_ttl_sec=60.0)   # enrollments, student_profiles


def is_miss(value: Any) -> bool:
    """Helper so call sites don't import the private sentinel."""
    return value is _MISS


__all__ = [
    "DYNAMIC_CACHE",
    "STATIC_CACHE",
    "TTLCache",
    "is_miss",
]
