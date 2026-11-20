"""Keep SSE alive during bounded inference and cancel abandoned work."""

import asyncio


async def with_progress(awaitable, content, *, interval=12):
    task = asyncio.create_task(awaitable)
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=interval)
            if not done:
                yield {'type': 'status', 'content': content}
        yield {'type': 'result', 'value': await task}
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
