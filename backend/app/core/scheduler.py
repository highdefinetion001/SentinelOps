"""Single-process fixed-interval collection loop used by the Monitor lifespan."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


async def collection_loop(collect: Callable[[], Awaitable[object]], interval_seconds: int) -> None:
    """Run one scan at a time, immediately at startup and then after each interval."""
    while True:
        try:
            await collect()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Scheduled collection failed; the next interval will retry")
        await asyncio.sleep(interval_seconds)
