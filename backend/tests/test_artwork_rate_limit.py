import asyncio

import pytest

from app.artwork.rate_limit import TokenBucketLimiter


@pytest.mark.anyio
async def test_limits_concurrency_to_cap() -> None:
    limiter = TokenBucketLimiter(rate_per_sec=1000.0, max_concurrency=2)
    active = 0
    peak = 0

    async def work() -> None:
        nonlocal active, peak
        async with limiter.slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

    await asyncio.gather(*(work() for _ in range(6)))
    assert peak <= 2


@pytest.mark.anyio
async def test_token_bucket_serializes_to_rate() -> None:
    # 50/sec -> ~20ms spacing. Four calls past the initial token must take a
    # measurable amount of time (not all fire instantly).
    limiter = TokenBucketLimiter(rate_per_sec=50.0, max_concurrency=10)
    loop = asyncio.get_running_loop()
    start = loop.time()

    async def work() -> None:
        async with limiter.slot():
            pass

    await asyncio.gather(*(work() for _ in range(5)))
    elapsed = loop.time() - start
    # 5 calls at 50/sec: first is free, remaining 4 cost ~80ms total.
    assert elapsed >= 0.05


@pytest.mark.anyio
async def test_releases_concurrency_on_exception() -> None:
    limiter = TokenBucketLimiter(rate_per_sec=1000.0, max_concurrency=1)

    slot = limiter.slot()  # creation only; the slot is acquired on enter, inside the block
    with pytest.raises(ValueError, match="boom"):
        async with slot:
            raise ValueError("boom")

    # Slot must be free again after the exception.
    async with limiter.slot():
        pass
