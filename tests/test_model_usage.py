import asyncio
import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from app.core.model_usage import (
    OPENAI_MODELS,
    UsageRecorder,
    capture_usage,
    current_usage,
)


def result(input_tokens=100, output_tokens=20, *, reasoning=0, cache_read=0,
           cache_write=0, response_metadata=None):
    message = AIMessage(
        content='Synthetic private answer that must not be recorded',
        usage_metadata={
            'input_tokens': input_tokens, 'output_tokens': output_tokens,
            'total_tokens': input_tokens + output_tokens,
            'input_token_details': {'cache_read': cache_read, 'cache_creation': cache_write},
            'output_token_details': {'reasoning': reasoning},
        },
        response_metadata=response_metadata or {},
    )
    return LLMResult(generations=[[ChatGeneration(message=message)]])


async def record(recorder, response=None, *, tags=None, invocation_params=None):
    run_id = uuid4()
    await recorder.on_chat_model_start(
        {}, [], run_id=run_id, tags=tags,
        invocation_params=invocation_params or {},
    )
    await recorder.on_llm_end(response if response is not None else result(), run_id=run_id)


class ModelUsageTests(unittest.IsolatedAsyncioTestCase):
    def test_prices_export_only_the_three_supported_openai_models(self):
        self.assertEqual(set(OPENAI_MODELS), {'gpt-6-luna', 'gpt-6.1-terra', 'gpt-6.2-sol'})
        self.assertIsInstance(UsageRecorder('openai', 'gpt-6-luna'), AsyncCallbackHandler)

    async def test_reasoning_is_included_in_output_and_not_charged_twice(self):
        with capture_usage() as usage:
            await record(UsageRecorder('openai', 'gpt-6-luna'),
                         result(1_000_000, 1_000_000, reasoning=800_000),
                         tags=['schema:AgentPlan'])
            snapshot = usage.snapshot()
        self.assertEqual(snapshot['callCount'], 1)
        self.assertEqual(snapshot['reasoningTokens'], 800_000)
        self.assertEqual(snapshot['outputTokens'], 1_000_000)
        self.assertEqual(Decimal(str(snapshot['estimatedCostUsd'])), Decimal('0.6'))
        call = snapshot['calls'][0]
        self.assertEqual(call['stage'], 'AgentPlan')
        self.assertEqual(call['status'], 'completed')
        self.assertGreaterEqual(call['latencyMs'], 0)
        self.assertIsNone(current_usage())

    async def test_standard_model_prices_are_applied_with_decimal_precision(self):
        for model, expected in [('gpt-6-luna', '0.6'), ('gpt-6.1-terra', '6'), ('gpt-6.2-sol', '12')]:
            with self.subTest(model=model), capture_usage() as usage:
                await record(UsageRecorder('openai', model), result(1_000_000, 1_000_000))
                self.assertEqual(Decimal(str(usage.snapshot()['estimatedCostUsd'])), Decimal(expected))

    async def test_cached_input_is_subtracted_from_regular_input(self):
        with capture_usage() as usage:
            await record(UsageRecorder('openai', 'gpt-6-luna'),
                         result(1_000_000, 100_000, reasoning=60_000, cache_read=100_000))
            snapshot = usage.snapshot()
        self.assertEqual(snapshot['cachedInputTokens'], 100_000)
        self.assertEqual(Decimal(str(snapshot['estimatedCostUsd'])), Decimal('0.141'))

    async def test_explicit_cache_write_lifetimes_are_priced_separately(self):
        raw = {'usage': {'input_tokens': 1_000_000, 'output_tokens': 100_000,
            'input_tokens_details': {'cached_tokens': 100_000,
                'cache_creation': {'ephemeral_30m_input_tokens': 40_000,
                                   'ephemeral_12h_input_tokens': 60_000}}}}
        with capture_usage() as usage:
            await record(UsageRecorder('openai', 'gpt-6-luna'),
                         result(1_000_000, 100_000, cache_read=100_000,
                                cache_write=100_000, response_metadata=raw))
            snapshot = usage.snapshot()
        self.assertEqual(snapshot['cacheWriteTokens'], 100_000)
        self.assertEqual(Decimal(str(snapshot['estimatedCostUsd'])), Decimal('0.151'))
        self.assertEqual(snapshot['calls'][0]['cacheWrite12hTokens'], 60_000)
        self.assertEqual(snapshot['calls'][0]['cacheWrite30mTokens'], 40_000)

    async def test_unknown_write_lifetime_uses_a_labeled_conservative_estimate(self):
        with capture_usage() as usage:
            await record(UsageRecorder('openai', 'gpt-6-luna'),
                         result(1_000_000, 0, cache_write=100_000))
            snapshot = usage.snapshot()
        self.assertEqual(Decimal(str(snapshot['estimatedCostUsd'])), Decimal('0.115'))
        self.assertIn('conservative', snapshot['calls'][0]['costBasis'])
        self.assertIn('estimate', snapshot['costBasis'])

    async def test_openai_native_cache_write_details_preserve_both_ttls(self):
        raw = {'usage': {'input_tokens': 1_000_000, 'output_tokens': 100_000,
            'input_tokens_details': {'cached_tokens': 100_000, 'cache_write_tokens': 100_000,
                'cache_write_tokens_details': {'ttl_30m_tokens': 40_000, 'ttl_12h_tokens': 60_000}}}}
        with capture_usage() as usage:
            await record(UsageRecorder('openai', 'gpt-6-luna'),
                         result(1_000_000, 100_000, cache_read=100_000,
                                cache_write=100_000, response_metadata=raw))
            snapshot = usage.snapshot()
        self.assertEqual(Decimal(str(snapshot['estimatedCostUsd'])), Decimal('0.151'))
        self.assertEqual(snapshot['calls'][0]['cacheWrite12hTokens'], 60_000)
        self.assertEqual(snapshot['calls'][0]['cacheWrite30mTokens'], 40_000)
        self.assertNotIn('conservative', snapshot['costBasis'])

    async def test_nested_native_ollama_result_counts_tokens_and_has_no_api_charge(self):
        native = LLMResult(generations=[[ChatGeneration(message=AIMessage(
            content='not persisted', response_metadata={'prompt_eval_count': 32, 'eval_count': 9},
        ))]])
        with capture_usage() as usage:
            await record(UsageRecorder('ollama', 'qwen3.5:9b'), native)
            snapshot = usage.snapshot()
        self.assertEqual(snapshot['inputTokens'], 32)
        self.assertEqual(snapshot['outputTokens'], 9)
        self.assertEqual(snapshot['estimatedCostUsd'], 0)
        self.assertEqual(snapshot['costBasis'], 'local_no_api_charge')

    async def test_response_metadata_usage_is_a_fallback(self):
        response = LLMResult(generations=[[ChatGeneration(message=AIMessage(content='',
            response_metadata={'usage': {'input_tokens': 1_000_000, 'output_tokens': 200_000,
                'input_tokens_details': {'cached_tokens': 100_000},
                'output_tokens_details': {'reasoning_tokens': 150_000}}}))]])
        with capture_usage() as usage:
            await record(UsageRecorder('openai', 'gpt-6-luna'), response,
                         invocation_params={'text': {'format': {'type': 'json_schema', 'name': 'ReportDraft'}}})
            snapshot = usage.snapshot()
        self.assertEqual(snapshot['reasoningTokens'], 150_000)
        self.assertEqual(snapshot['calls'][0]['stage'], 'ReportDraft')
        self.assertEqual(Decimal(str(snapshot['estimatedCostUsd'])), Decimal('0.191'))

    async def test_llm_output_usage_and_chat_response_format_stage_are_supported(self):
        response = LLMResult(generations=[], llm_output={'token_usage': {
            'prompt_tokens': 10, 'completion_tokens': 5,
            'completion_tokens_details': {'reasoning_tokens': 3}}})
        with capture_usage() as usage:
            run_id = uuid4()
            recorder = UsageRecorder('openai', 'gpt-6-luna')
            await recorder.on_llm_start({}, [], run_id=run_id,
                invocation_params={'response_format': {'json_schema': {'name': 'InvestigationDecision'}}})
            await recorder.on_llm_end(response, run_id=run_id)
            snapshot = usage.snapshot()
        self.assertEqual(snapshot['inputTokens'], 10)
        self.assertEqual(snapshot['reasoningTokens'], 3)
        self.assertEqual(snapshot['calls'][0]['stage'], 'InvestigationDecision')

    async def test_missing_usage_and_unknown_model_have_unknown_cost(self):
        for recorder, response in [
            (UsageRecorder('openai', 'gpt-6-luna'), LLMResult(generations=[])),
            (UsageRecorder('openai', 'unpriced-model'), result()),
            (UsageRecorder('openai_compatible', 'qwen'), result()),
        ]:
            with self.subTest(model=recorder), capture_usage() as usage:
                await record(recorder, response)
                self.assertIsNone(usage.snapshot()['estimatedCostUsd'])

    async def test_failed_or_inflight_calls_are_counted_and_not_assumed_free(self):
        recorder = UsageRecorder('openai', 'gpt-6-luna')
        secret = 'Bearer fake-private-key; Synthetic confidential prompt'
        with capture_usage() as usage:
            run_id = uuid4()
            await recorder.on_chat_model_start({'secret': secret}, [[secret]], run_id=run_id)
            self.assertEqual(usage.snapshot()['callCount'], 1)
            self.assertIsNone(usage.snapshot()['estimatedCostUsd'])
            await recorder.on_llm_error(TimeoutError(secret), run_id=run_id)
            snapshot = usage.snapshot()
        self.assertEqual(snapshot['calls'][0]['status'], 'error')
        self.assertEqual(snapshot['calls'][0]['errorType'], 'TimeoutError')
        self.assertIsNone(snapshot['estimatedCostUsd'])
        self.assertNotIn(secret, json.dumps(snapshot))

    async def test_snapshot_contains_no_message_or_prompt_and_is_detached(self):
        with capture_usage() as usage:
            await record(UsageRecorder('openai', 'gpt-6-luna'))
            snapshot = usage.snapshot()
            encoded = json.dumps(snapshot)
            snapshot['calls'][0]['inputTokens'] = 999
            self.assertEqual(usage.snapshot()['inputTokens'], 100)
        self.assertNotIn('Synthetic private answer', encoded)
        self.assertNotIn('prompt', encoded)
        self.assertNotIn('content', encoded)

    async def test_nested_scopes_restore_the_parent_and_outside_callbacks_are_noops(self):
        recorder = UsageRecorder('openai', 'gpt-6-luna')
        await record(recorder)
        self.assertIsNone(current_usage())
        with capture_usage() as outer:
            await record(recorder, result(1, 2))
            with capture_usage() as inner:
                await record(recorder, result(3, 4))
            self.assertIs(current_usage(), outer)
            self.assertEqual(inner.snapshot()['inputTokens'], 3)
            self.assertEqual(outer.snapshot()['inputTokens'], 1)
        self.assertIsNone(current_usage())

    async def test_two_concurrent_scopes_share_no_calls_even_with_a_single_recorder(self):
        recorder = UsageRecorder('openai', 'gpt-6-luna')
        started = asyncio.Event()
        starts = 0

        async def turn(tokens):
            nonlocal starts
            with capture_usage() as usage:
                run_id = uuid4()
                await recorder.on_chat_model_start({}, [], run_id=run_id)
                starts += 1
                if starts == 2:
                    started.set()
                await started.wait()
                await recorder.on_llm_end(result(tokens, 2), run_id=run_id)
                return usage.snapshot()

        first, second = await asyncio.gather(turn(7), turn(13))
        self.assertEqual([first['callCount'], second['callCount']], [1, 1])
        self.assertEqual([first['inputTokens'], second['inputTokens']], [7, 13])
        self.assertIsNone(current_usage())


class BookAgentModelUsageTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def settings():
        return SimpleNamespace(llm_provider='openai', llm_model='gpt-6-luna',
            release_revision=None, llm_reasoning=False, planner_reasoning=False,
            intent_reasoning=False, llm_reasoning_effort='none', ollama_num_ctx=8192,
            ollama_num_gpu=None, ollama_keep_alive='60s')

    @staticmethod
    def agent():
        from app.agent.book_agent import BookAgent
        agent = BookAgent.__new__(BookAgent)
        agent.memory = AsyncMock()
        agent.memory.ensure_session.side_effect = lambda **kwargs: kwargs['first_message']
        agent.memory.get_recent_messages.return_value = []
        return agent

    async def test_full_stream_round_counts_child_tasks_and_saves_provider_metadata(self):
        from app.schemas.plan import AgentPlan
        recorder = UsageRecorder('openai', 'gpt-6-luna')
        agent = self.agent()
        both_started = asyncio.Event()
        count = 0

        async def create_plan(message, **kwargs):
            nonlocal count
            run_id = uuid4()
            await recorder.on_chat_model_start({}, [], run_id=run_id, tags=['schema:AgentPlan'])
            count += 1
            if count == 2:
                both_started.set()
            await both_started.wait()
            await recorder.on_llm_end(result(int(message), 4), run_id=run_id)
            return AgentPlan(intent='unknown')

        agent._create_plan = create_plan

        async def consume(message):
            return [event async for event in agent.stream_chat('synthetic', message)]

        with patch('app.agent.book_agent.get_settings', return_value=self.settings()):
            streams = await asyncio.gather(consume('11'), consume('29'))
        self.assertTrue(all(events[-1]['type'] == 'done' for events in streams))
        saved = {call.kwargs['session_id']: call.kwargs['metadata']
                 for call in agent.memory.add_message.await_args_list if call.kwargs['role'] == 'assistant'}
        self.assertEqual(saved['11']['modelUsage']['inputTokens'], 11)
        self.assertEqual(saved['29']['modelUsage']['inputTokens'], 29)
        self.assertEqual(saved['11']['modelUsage']['callCount'], 1)
        self.assertIsNone(saved['11']['model']['planningTemperature'])
        self.assertEqual(saved['11']['model']['api'], 'responses')
        self.assertEqual(saved['11']['model']['provider'], 'openai')
        self.assertIsNone(current_usage())

    async def test_stream_can_close_in_a_different_task_without_leaking_or_resetting_its_scope(self):
        agent = self.agent()
        stream = agent.stream_chat('synthetic', 'closing')
        with capture_usage() as outer:
            self.assertEqual((await anext(stream))['type'], 'status')
            self.assertIs(current_usage(), outer)

            async def close_elsewhere():
                with capture_usage() as cleanup_scope:
                    await stream.aclose()
                    self.assertIs(current_usage(), cleanup_scope)

            await asyncio.create_task(close_elsewhere())
            self.assertIs(current_usage(), outer)
        self.assertIsNone(current_usage())

    async def test_closing_at_a_progress_event_awaits_child_model_cancellation(self):
        from app.agent.progress import with_progress
        agent = self.agent()
        cancelled = asyncio.Event()

        async def create_plan(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        agent._create_plan = create_plan
        stream = agent.stream_chat('synthetic', 'close-progress')

        def fast_progress(work, content):
            return with_progress(work, content, interval=.001)

        with patch('app.agent.book_agent.with_progress', fast_progress):
            while (await anext(stream)).get('content') != '正在理解问题与已有会话':
                pass
            await asyncio.create_task(stream.aclose())
            self.assertTrue(cancelled.is_set())
        self.assertIsNone(current_usage())

    async def test_cancelled_stream_finishes_its_scope_and_does_not_capture_the_next_turn(self):
        from app.schemas.plan import AgentPlan
        recorder = UsageRecorder('openai', 'gpt-6-luna')
        agent = self.agent()
        started = asyncio.Event()
        scopes = []

        async def create_plan(**kwargs):
            scopes.append(current_usage())
            run_id = uuid4()
            await recorder.on_chat_model_start({}, [], run_id=run_id)
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                await recorder.on_llm_error(exc, run_id=run_id)
                raise
            return AgentPlan(intent='unknown')

        agent._create_plan = create_plan

        async def consume():
            return [event async for event in agent.stream_chat('synthetic', 'cancelled')]

        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(current_usage())
        self.assertIsNotNone(scopes[0])
        before = scopes[0].snapshot()
        self.assertEqual(before['callCount'], 1)
        self.assertIsNone(before['estimatedCostUsd'])
        with capture_usage() as next_turn:
            await record(recorder, result(5, 6))
        self.assertEqual(next_turn.snapshot()['inputTokens'], 5)
        self.assertEqual(scopes[0].snapshot(), before)
