import json
import os
import unittest
from unittest.mock import patch

import httpx
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from app.core.config import get_settings
from app.core.llm import get_chat_model, get_planner_model


class ModelConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for factory in (get_settings, get_chat_model, get_planner_model):
            factory.cache_clear()
            self.addCleanup(factory.cache_clear)

    async def test_compatible_planner_uses_configured_gateway_and_json(self):
        received = []

        async def gateway(request):
            received.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "fixture",
                    "model": "qwen3.5:9b",
                    "choices": [{
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"expense_cents": 1250}'},
                    }],
                },
            )

        config = {
            "LLM_PROVIDER": "openai_compatible",
            "LLM_BASE_URL": "https://model.example.test/v1/",
            "LLM_MODEL": "qwen3.5:9b",
            "LLM_API_KEY": "fixture-key",
            "LLM_REASONING_EFFORT": "none",
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as http_client:
            with (
                patch.dict(os.environ, config, clear=True),
                patch(
                    "app.core.llm.ChatOpenAI",
                    side_effect=lambda **kwargs: ChatOpenAI(http_async_client=http_client, **kwargs),
                ),
            ):
                reply = await get_planner_model().ainvoke([HumanMessage(content="Fixture")])

        self.assertEqual(json.loads(reply.content), {"expense_cents": 1250})
        self.assertEqual(str(received[0].url), "https://model.example.test/v1/chat/completions")
        self.assertEqual(received[0].headers["authorization"], "Bearer fixture-key")
        body = json.loads(received[0].content)
        self.assertEqual(body["model"], "qwen3.5:9b")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["reasoning_effort"], "none")

    def test_existing_ollama_environment_still_configures_models(self):
        with patch.dict(os.environ, {
            "OLLAMA_BASE_URL": "http://localhost:11435",
            "OLLAMA_MODEL": "fixture:8b",
        }, clear=True):
            chat = get_chat_model()
            planner = get_planner_model()

        self.assertEqual(chat.base_url, "http://localhost:11435")
        self.assertEqual(chat.model, "fixture:8b")
        self.assertEqual(planner.format, "json")
