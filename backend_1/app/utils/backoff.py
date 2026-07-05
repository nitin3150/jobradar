import asyncio
import functools
import logging
import random

logger = logging.getLogger(__name__)


def with_backoff(max_retries: int = 3, base_delay: float = 1.0, max_delay: float = 60.0):
    """Decorator for async functions that retries with exponential backoff + jitter."""

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries:
                        logger.error(
                            f"{func.__name__} failed after {max_retries + 1} attempts: {e}"
                        )
                        raise
                    delay = min(
                        base_delay * (2**attempt) + random.uniform(0, 1), max_delay
                    )
                    logger.warning(
                        f"{func.__name__} attempt {attempt + 1} failed: {e}, "
                        f"retrying in {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)

        return wrapper

    return decorator
