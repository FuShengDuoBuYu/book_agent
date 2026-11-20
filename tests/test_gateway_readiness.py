import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import Response
from pydantic import SecretStr
from app.api.routes import health


class GatewayReadinessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        if hasattr(health, 'GatewayProbe'):
            health.gateway_probe = health.GatewayProbe()
        self.settings = SimpleNamespace(agent_mongodb_uri='mongodb://synthetic',
            llm_provider='openai', llm_base_url='https://fixture.invalid/v1',
            llm_model='gpt-6.1-terra', llm_api_key=SecretStr('synthetic-not-a-real-key'))
        self.result = MagicMock()
        self.result.json.return_value = {'data': [{'id': 'gpt-6.1-terra'}]}
        self.client = AsyncMock()
        self.client.get.return_value = self.result
        self.manager = AsyncMock()
        self.manager.__aenter__.return_value = self.client

    async def call(self):
        response = Response()
        body = await health.ready(response)
        return response, body

    async def test_official_api_probe_allows_measured_cold_connection_latency(self):
        with patch.object(health, 'get_settings', return_value=self.settings), \
             patch.object(health.httpx, 'AsyncClient', return_value=self.manager) as factory:
            response, body = await self.call()
        self.assertEqual(body['status'], 'ok')
        self.assertGreaterEqual(factory.call_args.kwargs['timeout'], 15)
        self.assertEqual(response.headers.get('cache-control'), 'no-store')

    async def test_repeated_success_does_not_create_a_cold_connection_per_request(self):
        with patch.object(health, 'get_settings', return_value=self.settings), \
             patch.object(health.httpx, 'AsyncClient', return_value=self.manager):
            for _ in range(3):
                _, body = await self.call()
                self.assertEqual(body['status'], 'ok')
        self.assertEqual(self.client.get.await_count, 1)

    async def test_concurrent_requests_share_the_same_probe(self):
        started = asyncio.Event()
        release = asyncio.Event()
        async def delayed(*args, **kwargs):
            started.set()
            await release.wait()
            return self.result
        self.client.get.side_effect = delayed
        with patch.object(health, 'get_settings', return_value=self.settings), \
             patch.object(health.httpx, 'AsyncClient', return_value=self.manager):
            calls = [asyncio.create_task(self.call()) for _ in range(3)]
            await started.wait()
            release.set()
            answers = await asyncio.gather(*calls)
        self.assertTrue(all(body['status'] == 'ok' for _, body in answers))
        self.assertEqual(self.client.get.await_count, 1)
    async def test_expired_success_does_not_hide_a_removed_model(self):
        with patch.object(health, 'get_settings', return_value=self.settings), \
             patch.object(health.httpx, 'AsyncClient', return_value=self.manager), \
             patch.object(health, 'monotonic', create=True, return_value=100) as clock:
            self.assertEqual((await self.call())[1]['status'], 'ok')
            clock.return_value = 131
            self.result.json.return_value = {'data': []}
            response, body = await self.call()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(body['status'], 'model_not_listed')
        self.assertEqual(self.client.get.await_count, 2)

    async def test_configuration_and_key_changes_bypass_prior_success(self):
        with patch.object(health, 'get_settings', return_value=self.settings), \
             patch.object(health.httpx, 'AsyncClient', return_value=self.manager):
            await self.call()
            self.settings.llm_api_key = SecretStr('different-synthetic-key')
            await self.call()
            self.settings.llm_base_url = 'https://another.invalid/v1'
            await self.call()
            self.settings.llm_model = 'not-listed'
            response, body = await self.call()
        self.assertEqual(self.client.get.await_count, 4)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(body['status'], 'model_not_listed')

    async def test_gateway_failure_stays_unready_and_can_recover_immediately(self):
        self.client.get.side_effect = [health.httpx.ReadTimeout('synthetic'), self.result]
        with patch.object(health, 'get_settings', return_value=self.settings), \
             patch.object(health.httpx, 'AsyncClient', return_value=self.manager):
            response, body = await self.call()
            self.assertEqual(response.status_code, 503)
            self.assertEqual(body, {'status': 'model_gateway_unavailable'})
            self.assertEqual((await self.call())[1]['status'], 'ok')
        self.assertEqual(self.client.get.await_count, 2)

    async def test_missing_memory_is_unready_even_after_cached_success(self):
        with patch.object(health, 'get_settings', return_value=self.settings), \
             patch.object(health.httpx, 'AsyncClient', return_value=self.manager):
            await self.call()
            self.settings.agent_mongodb_uri = None
            response, body = await self.call()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(body['status'], 'memory_not_configured')
        self.assertEqual(self.client.get.await_count, 1)

    async def test_native_ollama_keeps_local_timeout_and_tag_contract(self):
        self.settings.llm_provider = 'ollama'
        self.settings.llm_model = 'qwen3.5:9b'
        self.settings.llm_base_url = 'http://127.0.0.1:11434'
        self.result.json.return_value = {'models': [{'name': 'qwen3.5:9b'}]}
        with patch.object(health, 'get_settings', return_value=self.settings), \
             patch.object(health.httpx, 'AsyncClient', return_value=self.manager) as factory:
            response, body = await self.call()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['model'], 'qwen3.5:9b')
        self.assertEqual(factory.call_args.kwargs['timeout'], 5)
        self.assertEqual(self.client.get.await_args.args[0], 'http://127.0.0.1:11434/api/tags')
        self.assertEqual(self.client.get.await_args.kwargs['headers'], {})
    async def test_concurrent_failed_probe_is_shared_but_next_request_retries(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed_failure(*args, **kwargs):
            started.set()
            await release.wait()
            raise health.httpx.ReadTimeout('synthetic')
        self.client.get.side_effect = delayed_failure
        with patch.object(health, 'get_settings', return_value=self.settings), \
             patch.object(health.httpx, 'AsyncClient', return_value=self.manager):
            calls = [asyncio.create_task(self.call()) for _ in range(3)]
            await started.wait()
            release.set()
            answers = await asyncio.gather(*calls)
            self.assertTrue(all(response.status_code == 503 for response, _ in answers))
            self.assertEqual(self.client.get.await_count, 1)
            self.client.get.side_effect = None
            self.client.get.return_value = self.result
            self.assertEqual((await self.call())[1]['status'], 'ok')
        self.assertEqual(self.client.get.await_count, 2)

    async def test_one_cancelled_request_does_not_cancel_another_waiters_probe(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kwargs):
            started.set()
            await release.wait()
            return self.result
        self.client.get.side_effect = delayed
        with patch.object(health, 'get_settings', return_value=self.settings), \
             patch.object(health.httpx, 'AsyncClient', return_value=self.manager):
            first = asyncio.create_task(self.call())
            second = asyncio.create_task(self.call())
            await started.wait()
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            release.set()
            self.assertEqual((await second)[1]['status'], 'ok')
        self.assertEqual(self.client.get.await_count, 1)
