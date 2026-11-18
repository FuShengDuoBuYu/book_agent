import json
import os
import unittest
from unittest.mock import patch

import httpx
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from app.core.config import get_settings
from app.core.llm import get_chat_model, get_planner_model, get_structured_model


class ModelConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def test_native_planner_schema_requires_execution_not_only_an_intent_description(self):
        from app.tools.financial_analysis import ANALYSIS_TOOLS
        from app.tools.registry import PLANNER_TOOL_BY_NAME
        with patch.dict(os.environ, {'LLM_PROVIDER': 'ollama'}, clear=True):
            schema = get_planner_model().format
        self.assertTrue({'understanding', 'analyses', 'toolCalls', 'analysisWindow', 'finalInstruction'} <= set(schema.get('required', [])))
        analysis = schema['$defs']['AnalysisRequest']
        self.assertEqual(set(analysis['properties']['tool']['enum']), set(ANALYSIS_TOOLS))
        self.assertTrue({'tool', 'queryIds', 'metric', 'targetMonth'} <= set(analysis['required']))
        self.assertEqual(set(schema['$defs']['ToolCall']['properties']['toolName']['enum']), set(PLANNER_TOOL_BY_NAME))
        evidence = schema['$defs']['IntentUnderstanding']['properties']['evidenceNeeds']['items']['enum']
        self.assertTrue({'runway', 'savings', 'cashflow', 'data_quality', 'members', 'totals'} <= set(evidence))

    def test_planner_schema_requires_explicit_grouping_filters_and_horizon_fields(self):
        with patch.dict(os.environ, {'LLM_PROVIDER': 'ollama'}, clear=True):
            schema = get_planner_model().format
        self.assertTrue({'aggregate', 'changes', 'months'} <= set(schema['$defs']['AnalysisRequest']['required']))
        self.assertTrue({'groupBy', 'periods', 'filters'} <= set(schema['$defs']['AggregateSpec']['required']))
        self.assertTrue({'includeCategories', 'excludeCategories'} <= set(schema['$defs']['AggregateFilters']['required']))

    def test_structured_writer_has_an_independent_actual_output_budget(self):
        from app.schemas.report import ReportReview
        with patch.dict(os.environ, {'LLM_PROVIDER': 'ollama', 'PLANNER_NUM_PREDICT': '4096'}, clear=True):
            reviewer = get_structured_model(ReportReview, reasoning=False, num_predict=512)
            planner = get_planner_model()
        self.assertEqual(reviewer.bound.num_predict, 512)
        self.assertFalse(reviewer.bound.reasoning)
        self.assertEqual(planner.num_predict, 4096)
        self.assertFalse(planner.reasoning)

    def test_native_report_keeps_natural_expression_with_a_grounding_contract(self):
        from app.schemas.report import ReportDraft
        with patch.dict(os.environ, {'LLM_PROVIDER': 'ollama'}, clear=True):
            writer = get_structured_model(ReportDraft, reasoning=False, num_predict=2048)
        fields = writer.kwargs['format']['$defs']['ReportSection']['properties']
        self.assertIn('事实句', fields['analysis']['description'])
        self.assertNotIn('pattern', fields['analysis'])
        self.assertNotIn('pattern', fields['title'])

    def setUp(self):
        for factory in (get_settings, get_chat_model, get_planner_model, get_structured_model):
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
        self.assertEqual(planner.format["title"], "AgentPlan")
        self.assertIn("analysisWindow", planner.format["properties"])

    def test_analysis_reasoning_and_context_are_configurable(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "ollama", "LLM_MODEL": "fixture",
                                    "LLM_REASONING": "true", "OLLAMA_NUM_CTX": "32768"}, clear=True):
            chat = get_chat_model()
            planner = get_planner_model()
        self.assertTrue(chat.reasoning)
        self.assertFalse(planner.reasoning)
        self.assertEqual(chat.num_ctx, 32768)
        self.assertEqual(chat.keep_alive, '60s')

    def test_gpu_offload_can_leave_room_for_other_workloads(self):
        with patch.dict(os.environ, {'LLM_PROVIDER': 'ollama', 'OLLAMA_NUM_GPU': '24'}, clear=True):
            self.assertEqual(get_chat_model().num_gpu, 24)
