import asyncio

import pytest

from backend.app.core.scheduler import collection_loop


def test_collection_loop_runs_one_scan_at_a_time_and_cancels_cleanly() -> None:
    async def scenario() -> tuple[int, int]:
        active = 0
        maximum_active = 0
        starts = 0
        started = asyncio.Event()
        allow_finish = asyncio.Event()

        async def collect() -> None:
            nonlocal active, maximum_active, starts
            starts += 1
            active += 1
            maximum_active = max(maximum_active, active)
            started.set()
            await allow_finish.wait()
            active -= 1

        task = asyncio.create_task(collection_loop(collect, interval_seconds=3600))
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert starts == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return starts, maximum_active

    starts, maximum_active = asyncio.run(scenario())
    assert starts == 1
    assert maximum_active == 1


def test_collection_loop_retries_after_callback_failure() -> None:
    async def scenario() -> int:
        starts = 0
        second_started = asyncio.Event()

        async def collect() -> None:
            nonlocal starts
            starts += 1
            if starts == 1:
                raise RuntimeError("test failure")
            second_started.set()

        task = asyncio.create_task(collection_loop(collect, interval_seconds=1))
        try:
            await asyncio.wait_for(second_started.wait(), timeout=2)
            return starts
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert asyncio.run(scenario()) >= 2
