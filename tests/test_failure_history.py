import asyncio
from copy import deepcopy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from app.agent.analysis_context import latest_context
from app.agent.book_agent import BookAgent
from app.core.model_usage import UsageRecorder, current_usage
from app.schemas.plan import AgentPlan


FAILURE_MESSAGE = 'AI 账单处理失败，请稍后重试。'
SECRET = 'Bearer synthetic-private-token; private SDK request body'


class ProviderFailure(RuntimeError):
    pass


class FailureHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.agent = BookAgent.__new__(BookAgent)
        self.agent.memory = AsyncMock()
        self.agent.memory.ensure_session.return_value = 'resolved-session'
        self.agent.memory.get_recent_messages.return_value = []
        self.agent._create_plan = AsyncMock(side_effect=ProviderFailure(SECRET))
        settings = SimpleNamespace(llm_provider='openai', llm_model='gpt-6-luna',
            release_revision='a' * 40, llm_reasoning=False, planner_reasoning=False,
            intent_reasoning=False, llm_reasoning_effort='none', ollama_num_ctx=8192,
            ollama_num_gpu=None, ollama_keep_alive='60s')
        setting_patch = patch('app.agent.book_agent.get_settings', return_value=settings)
        setting_patch.start()
        self.addCleanup(setting_patch.stop)

    async def consume(self):
        return [event async for event in self.agent.stream_chat(
            'synthetic', 'Synthetic question', session_id='requested-session')]

    def assistant_writes(self):
        return [call.kwargs for call in self.agent.memory.add_message.await_args_list
                if call.kwargs['role'] == 'assistant']

    async def test_early_model_failure_saves_one_generic_reply_and_private_audit(self):
        with self.assertLogs('app.agent.book_agent', level='WARNING'):
            events = await self.consume()
        self.assertEqual(events[-1], {'type': 'error', 'content': FAILURE_MESSAGE})
        self.assertFalse(any(event['type'] in {'done', 'delta'} for event in events))
        saved = self.assistant_writes()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['session_id'], 'resolved-session')
        self.assertEqual(saved[0]['content'], FAILURE_MESSAGE)
        metadata = saved[0]['metadata']
        self.assertEqual(metadata['status'], 'error')
        self.assertEqual(metadata['errorType'], 'ProviderFailure')
        self.assertEqual(metadata['releaseRevision'], 'a' * 40)
        self.assertIsInstance(metadata['durationMs'], int)
        self.assertGreaterEqual(metadata['durationMs'], 0)
        self.assertEqual(metadata['modelUsage']['callCount'], 0)
        self.assertEqual(metadata['analysisContext'], {})
        self.assertIsNone(current_usage())

    async def test_failed_round_keeps_completed_call_usage_and_marks_failed_call_unknown(self):
        recorder = UsageRecorder('openai', 'gpt-6-luna')

        async def create_plan(**kwargs):
            first = uuid4()
            await recorder.on_chat_model_start({}, [], run_id=first, tags=['schema:IntentDecision'])
            await recorder.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(
                content='private model result', usage_metadata={'input_tokens': 100, 'output_tokens': 20,
                    'total_tokens': 120, 'output_token_details': {'reasoning': 7}},
            ))]]), run_id=first)
            second = uuid4()
            await recorder.on_chat_model_start({}, [], run_id=second, tags=['schema:AgentPlan'])
            await recorder.on_llm_error(ProviderFailure(SECRET), run_id=second)
            raise ProviderFailure(SECRET)

        self.agent._create_plan = create_plan
        with self.assertLogs('app.agent.book_agent', level='WARNING'):
            await self.consume()
        saved = self.assistant_writes()
        self.assertEqual(len(saved), 1)
        usage = saved[0]['metadata']['modelUsage']
        self.assertEqual(usage['callCount'], 2)
        self.assertEqual(usage['calls'][0]['inputTokens'], 100)
        self.assertEqual(usage['calls'][0]['outputTokens'], 20)
        self.assertEqual(usage['calls'][0]['reasoningTokens'], 7)
        self.assertEqual(usage['calls'][1]['status'], 'error')
        self.assertIsNone(usage['estimatedCostUsd'])

    async def test_failure_audit_and_warnings_do_not_store_exception_or_sdk_payloads(self):
        failure = ProviderFailure(SECRET)
        failure.request = SimpleNamespace(headers={'authorization': SECRET}, body=SECRET)
        failure.response = {'message': SECRET}
        self.agent._create_plan.side_effect = failure
        with self.assertLogs('app.agent.book_agent', level='WARNING') as logged:
            await self.consume()
        saved = self.assistant_writes()
        self.assertEqual(len(saved), 1)
        encoded = json.dumps(saved[0], ensure_ascii=False)
        self.assertNotIn(SECRET, encoded)
        self.assertNotIn(SECRET, '\n'.join(logged.output))
        self.assertNotIn('request', saved[0]['metadata'])
        self.assertNotIn('response', saved[0]['metadata'])
        self.assertNotIn('exception', saved[0]['metadata'])

    async def test_failure_preserves_confirmed_context_even_if_the_failed_planner_mutates_its_copy(self):
        prior = {'scope': [['search_personal_orders', '不限', '']], 'months': 12,
            'startingBalance': 10000, 'targetAmount': 80000,
            'changes': [{'metric': 'income', 'source': '球课', 'monthlyDelta': 1000}],
            'observedSourceDirections': {'球课': ['income']}}
        # An earlier failed audit already holds the trusted snapshot. Its
        # evidence/plan must not be reinterpreted as a newly completed query.
        self.agent.memory.get_recent_messages.return_value = [{
            'role': 'assistant', 'content': FAILURE_MESSAGE,
            'metadata': {'status': 'error', 'contextOrigin': 'prior_confirmed_turn',
                         'analysisContext': deepcopy(prior)},
        }]

        async def create_plan(previous_context, **kwargs):
            previous_context['months'] = 99
            previous_context['scope'][0][0] = 'search_family_orders'
            previous_context['observedSourceDirections'] = {'球课': ['expense']}
            raise ProviderFailure(SECRET)

        self.agent._create_plan = create_plan
        with self.assertLogs('app.agent.book_agent', level='WARNING'):
            await self.consume()
        saved = self.assistant_writes()
        self.assertEqual(len(saved), 1)
        metadata = saved[0]['metadata']
        self.assertEqual(metadata['contextOrigin'], 'prior_confirmed_turn')
        self.assertEqual(metadata['analysisContext'], prior)
        self.assertNotIn('plan', metadata)
        self.assertNotIn('evidence', metadata)
        self.assertEqual(latest_context([saved[0]]), prior)
        self.agent.memory.ensure_session.assert_awaited_once_with(
            phone_num='synthetic', session_id='requested-session', first_message='Synthetic question',
            book_scope='personal', new_session=False)
        self.agent.memory.get_recent_messages.assert_awaited_once_with(
            phone_num='synthetic', session_id='resolved-session', limit=12)

    async def test_no_context_is_invented_from_an_unconfirmed_failed_plan(self):
        self.agent.memory.get_recent_messages.return_value = [{
            'role': 'assistant', 'content': FAILURE_MESSAGE,
            'metadata': {'status': 'error', 'analysisContext': {'months': 999,
                'observedSourceDirections': {'unconfirmed': ['income']}}},
        }]
        with self.assertLogs('app.agent.book_agent', level='WARNING'):
            await self.consume()
        saved = self.assistant_writes()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['metadata']['analysisContext'], {})
        self.assertEqual(latest_context([saved[0]]), {})

    async def test_error_history_storage_failure_keeps_the_original_sse_error(self):
        async def add_message(**kwargs):
            if kwargs['role'] == 'assistant':
                raise OSError(SECRET)

        self.agent.memory.add_message.side_effect = add_message
        with self.assertLogs('app.agent.book_agent', level='WARNING') as logged:
            events = await self.consume()
        self.assertEqual(events[-1], {'type': 'error', 'content': FAILURE_MESSAGE})
        self.assertEqual(len(self.assistant_writes()), 1)
        self.assertEqual(self.assistant_writes()[0]['metadata']['errorType'], 'ProviderFailure')
        self.assertIn('OSError', '\n'.join(logged.output))
        self.assertNotIn(SECRET, '\n'.join(logged.output))

    async def test_session_failure_does_not_create_a_failure_message(self):
        self.agent.memory.ensure_session.side_effect = OSError(SECRET)
        with self.assertLogs('app.agent.book_agent', level='WARNING'):
            events = await self.consume()
        self.assertEqual(events[-1], {'type': 'error', 'content': FAILURE_MESSAGE})
        self.agent.memory.add_message.assert_not_awaited()

    async def test_user_message_failure_does_not_add_an_assistant_failure_record(self):
        self.agent.memory.add_message.side_effect = OSError(SECRET)
        with self.assertLogs('app.agent.book_agent', level='WARNING'):
            events = await self.consume()
        self.assertEqual(events[-1], {'type': 'error', 'content': FAILURE_MESSAGE})
        self.assertEqual(self.assistant_writes(), [])
        self.agent._create_plan.assert_not_awaited()

    async def test_history_failure_before_user_write_does_not_add_a_failure_record(self):
        self.agent.memory.get_recent_messages.side_effect = OSError(SECRET)
        with self.assertLogs('app.agent.book_agent', level='WARNING'):
            events = await self.consume()
        self.assertEqual(events[-1], {'type': 'error', 'content': FAILURE_MESSAGE})
        self.agent.memory.add_message.assert_not_awaited()

    async def test_partial_stream_failure_persists_generic_failure_instead_of_unfinished_text(self):
        self.agent._create_plan = AsyncMock(return_value=AgentPlan(intent='chat', needsTools=False))

        async def stream_reply(_):
            yield SimpleNamespace(content='Unfinished private model text')
            raise ProviderFailure(SECRET)

        self.agent.chain = SimpleNamespace(astream=stream_reply)
        with self.assertLogs('app.agent.book_agent', level='WARNING'):
            events = await self.consume()
        self.assertTrue(any(event['type'] == 'delta' for event in events))
        self.assertEqual(events[-1], {'type': 'error', 'content': FAILURE_MESSAGE})
        self.assertEqual([row['content'] for row in self.assistant_writes()], [FAILURE_MESSAGE])

    async def test_cancelled_turn_still_propagates_cancellation_without_a_failure_record(self):
        started = asyncio.Event()

        async def create_plan(**kwargs):
            started.set()
            await asyncio.Event().wait()

        self.agent._create_plan = create_plan
        task = asyncio.create_task(self.consume())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.assistant_writes(), [])
        self.assertIsNone(current_usage())


class FailureContextTrustTests(unittest.TestCase):
    def test_confirmed_failure_snapshot_keeps_directions_without_rederiving_failed_plan(self):
        context = {'scope': [['search_personal_orders', '不限', '']], 'months': 6,
                   'observedSourceDirections': {'球课': ['income']}}
        failure = {'role': 'assistant', 'metadata': {'status': 'error',
            'contextOrigin': 'prior_confirmed_turn', 'analysisContext': context,
            'plan': {'toolCalls': [{'id': 'failed', 'toolName': 'search_family_orders'}]},
            'evidence': {'ok': True, 'analysisResults': []}}}
        restored = latest_context([failure])
        self.assertEqual(restored, context)
        restored['scope'][0][0] = 'changed'
        self.assertEqual(context['scope'][0][0], 'search_personal_orders')

    def test_unmarked_error_context_is_not_a_confirmed_snapshot(self):
        self.assertEqual(latest_context([{'role': 'assistant', 'metadata': {
            'status': 'error', 'analysisContext': {'months': 999}}}]), {})

    def test_successful_turn_does_not_trust_an_unproven_direction_cache(self):
        self.assertEqual(latest_context([{'role': 'assistant', 'metadata': {
            'contextOrigin': 'prior_confirmed_turn',
            'analysisContext': {'months': 6, 'observedSourceDirections': {'unknown': ['income']}}}}]), {'months': 6})
