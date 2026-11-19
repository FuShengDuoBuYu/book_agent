import json
import os
import unittest
from unittest.mock import patch

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.config import get_settings
from app.core.llm import get_chat_model, get_planner_model, get_structured_model


class Probe(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sentinel: str = 'BOOK_AGENT_PROBE'


class OpenAIAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for f in (get_settings, get_chat_model, get_planner_model, get_structured_model):
            f.cache_clear()
            self.addCleanup(f.cache_clear)

    def config(self, **extra):
        return patch.dict(os.environ, {'LLM_PROVIDER': 'openai', 'LLM_API_KEY': 'fixture-key',
            'LLM_MODEL': 'gpt-6-luna', 'LLM_REASONING_EFFORT': 'none', **extra}, clear=True)

    def test_defaults_to_official_endpoint_and_rejects_model_above_cost_ceiling(self):
        with self.config():
            self.assertEqual(get_settings().llm_base_url, 'https://api.openai.com/v1')
            self.assertNotIn('fixture-key', repr(get_settings()))
        get_settings.cache_clear()
        with self.config(LLM_MODEL='gpt-6.2-astra'):
            with self.assertRaises(ValidationError):
                get_settings()

    async def test_strict_responses_contract_and_plain_json_content(self):
        received = []

        async def handler(request):
            received.append(json.loads(request.content))
            return httpx.Response(200, json={
                'id': 'resp_fixture', 'object': 'response', 'created_at': 1, 'model': 'gpt-6-luna',
                'status': 'completed', 'error': None, 'incomplete_details': None,
                'output': [{'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': '{"sentinel":"BOOK_AGENT_PROBE"}', 'annotations': []}]}],
                'usage': {'input_tokens': 45, 'input_tokens_details': {'cached_tokens': 0},
                          'output_tokens': 17, 'output_tokens_details': {'reasoning_tokens': 0}, 'total_tokens': 62}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.config(), patch('app.core.llm._openai_http_client', return_value=client):
                response = await get_structured_model(Probe, reasoning=False, num_predict=128).ainvoke([
                    ('human', '只回答准备好了。')])
        self.assertEqual(json.loads(response.content), {'sentinel': 'BOOK_AGENT_PROBE'})
        body = received[0]
        self.assertNotIn('temperature', body)
        self.assertFalse(body['store'])
        self.assertEqual(body['reasoning']['effort'], 'none')
        self.assertEqual(body['max_output_tokens'], 128)
        fmt = body['text']['format']
        self.assertEqual(fmt['type'], 'json_schema')
        self.assertTrue(fmt['strict'])
        self.assertEqual(fmt['schema']['required'], ['sentinel'])
        self.assertFalse(fmt['schema']['additionalProperties'])
