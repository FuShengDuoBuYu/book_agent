import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.agent.analysis_context import latest_context
from app.agent.book_agent import BookAgent
from app.agent.planner import QueryPlanner


class PlanningFailureHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_exhausted_planner_timeouts_keep_the_last_confirmed_context(self):
        private_detail = 'fictional timeout with private provider details'
        prior = {'months': 4, 'startingBalance': 10000, 'targetAmount': 20000,
                 'scope': [['search_personal_orders', '不限', '']]}
        history = [{'role': 'assistant', 'content': '已确认储蓄条件。',
                    'metadata': {'analysisContext': deepcopy(prior)}}]
        planner = QueryPlanner.__new__(QueryPlanner)
        planner.chain = SimpleNamespace(ainvoke=AsyncMock(side_effect=asyncio.TimeoutError(private_detail)))
        agent = BookAgent.__new__(BookAgent)
        agent.planner = planner
        agent.memory = AsyncMock()
        agent.memory.ensure_session.return_value = 'synthetic-session'
        agent.memory.get_recent_messages.return_value = history
        settings = SimpleNamespace(planner_timeout_seconds=1, llm_provider='openai', llm_model='gpt-6-luna',
            release_revision='a' * 40, llm_reasoning=False, planner_reasoning=False,
            intent_reasoning=False, llm_reasoning_effort='none', ollama_num_ctx=8192,
            ollama_num_gpu=None, ollama_keep_alive='60s')

        with (patch('app.agent.planner.get_settings', return_value=settings),
              patch('app.agent.book_agent.get_settings', return_value=settings)):
            async for _ in agent.stream_chat('synthetic', '照刚才确认的储蓄条件再算一次。'):
                pass

        self.assertEqual(planner.chain.ainvoke.await_count, 2)
        saved, = [call.kwargs for call in agent.memory.add_message.await_args_list
                  if call.kwargs['role'] == 'assistant']
        self.assertEqual(latest_context([*history, saved]), prior)
        self.assertEqual(saved['metadata']['status'], 'error')
        self.assertEqual(saved['metadata']['contextOrigin'], 'prior_confirmed_turn')
        self.assertIn('modelUsage', saved['metadata'])
        self.assertNotIn(private_detail, json.dumps(saved, ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
