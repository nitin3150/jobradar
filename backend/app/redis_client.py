from datetime import timedelta

import redis.asyncio as aioredis

from app.config import settings

DEDUP_TTL = timedelta(days=30)

redis_pool: aioredis.Redis | None = None


async def init_redis() -> aioredis.Redis:
    global redis_pool
    redis_pool = aioredis.from_url(
        settings.redis_url,
        max_connections=50,
        decode_responses=True,
    )
    return redis_pool


async def close_redis():
    global redis_pool
    if redis_pool:
        await redis_pool.aclose()
        redis_pool = None


def get_redis() -> aioredis.Redis:
    if redis_pool is None:
        raise RuntimeError("Redis not initialized. Call init_redis() first.")
    return redis_pool


_redis_instance = None


async def get_redis_async() -> aioredis.Redis:
    """Async lazy-init accessor — awaitable alternative to get_redis()."""
    global _redis_instance
    if _redis_instance is None:
        _redis_instance = await init_redis()
    return _redis_instance


async def is_duplicate(name_slug: str, funding_date: str) -> bool:
    r = get_redis()
    key = f"company:{name_slug}:{funding_date}"
    return await r.exists(key) > 0


async def mark_seen(name_slug: str, funding_date: str) -> None:
    r = get_redis()
    key = f"company:{name_slug}:{funding_date}"
    await r.setex(key, DEDUP_TTL, "1")
