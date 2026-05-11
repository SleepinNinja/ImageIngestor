"""asyncpg connection pool management.

The pool is created once during the FastAPI lifespan and stored on app.state
so every request handler can share it without re-connecting.
"""

import asyncpg

from app.config import settings

_pool: asyncpg.Pool | None = None


async def create_pool() -> asyncpg.Pool:
    """Create and return the global asyncpg connection pool.

    Called once from the FastAPI lifespan hook. Returns the same pool on
    repeated calls so it is safe to call from tests that set up their own pool.
    """
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.DATABASE_URL,
            min_size=2,
            max_size=10,
        )
    return _pool


async def close_pool() -> None:
    """Gracefully close the connection pool on shutdown.

    Waits for in-flight queries to complete before closing.
    """
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
