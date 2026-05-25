"""Async token-bucket rate limiter with a concurrency cap.

Two independent throttles applied to every outbound Deezer call:

* a **token bucket** that paces calls to ``rate_per_sec`` (smooths the
  first-load burst well under Deezer's ~50 req/5s), and
* a **semaphore** that caps how many requests are in flight at once.

Acquire both via :meth:`slot`, an async context manager that holds the
concurrency slot for the duration of the request and releases it (even on
error). The token is consumed up front and not returned.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class TokenBucketLimiter:
    def __init__(self, *, rate_per_sec: float, max_concurrency: int) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self._interval = 1.0 / rate_per_sec
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._lock = asyncio.Lock()
        # Earliest time at which the next token may be issued.
        self._next_available = 0.0

    async def _take_token(self) -> None:
        # Serialize token issuance so concurrent waiters get distinct slots.
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            scheduled = max(now, self._next_available)
            self._next_available = scheduled + self._interval
            wait = scheduled - now
        if wait > 0:
            await asyncio.sleep(wait)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Acquire a rate token and a concurrency slot for one outbound call."""
        await self._semaphore.acquire()
        try:
            await self._take_token()
            yield
        finally:
            self._semaphore.release()
