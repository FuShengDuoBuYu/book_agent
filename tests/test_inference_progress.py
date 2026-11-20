import asyncio
import unittest


class InferenceProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_keeps_connection_alive_and_returns_the_actual_result(self):
        from app.agent.progress import with_progress
        async def work():
            await asyncio.sleep(.025)
            return {'actual': 7}
        events = [event async for event in with_progress(work(), '正在查证', interval=.005)]
        self.assertTrue(any(event['type'] == 'status' for event in events))
        self.assertEqual(events[-1]['value'], {'actual': 7})

    async def test_disconnected_stream_cancels_its_inference_task(self):
        from app.agent.progress import with_progress
        cancelled = asyncio.Event()
        async def work():
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        stream = with_progress(work(), '正在查证', interval=.001)
        self.assertEqual((await anext(stream))['type'], 'status')
        await stream.aclose()
        self.assertTrue(cancelled.is_set())
