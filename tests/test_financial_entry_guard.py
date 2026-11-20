import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent.book_agent import BookAgent
from app.schemas.plan import AgentPlan
from scripts.evaluate_analysis import FixtureLedger, FixtureMemory


class FinancialEntryGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_financial_plan_cannot_stream_unverified_prose(self):
        agent = BookAgent(FixtureLedger())
        agent.memory = FixtureMemory()
        calls = []

        async def unsafe_writer(_):
            calls.append(True)
            yield SimpleNamespace(content='已确认当前存款99999元。')

        agent.chain = SimpleNamespace(astream=unsafe_writer)
        plan = AgentPlan.model_validate({'intent': 'analyze_orders', 'analysisType': 'total_spending',
            'needsTools': True, 'analyses': [], 'toolCalls': [{'toolName': 'search_personal_orders'}]})
        with patch.object(agent, '_create_plan', AsyncMock(return_value=plan)):
            reply, _ = await agent.chat('synthetic-owner', '总共支出多少？')
        self.assertFalse(calls)
        self.assertNotIn('99999', reply)
        self.assertIn('支出', reply)
        metadata = agent.memory.messages[-1]['metadata']
        self.assertEqual(metadata['validation']['mechanism'], 'computed_query_summary')
        self.assertEqual(metadata['validation']['status'], 'grounded')

    async def test_unexecutable_analysis_never_falls_through_to_the_chat_writer(self):
        agent = BookAgent(FixtureLedger())
        agent.memory = FixtureMemory()
        agent.chain = SimpleNamespace(astream=lambda _: self.fail('financial path reached free writer'))
        plan = AgentPlan(intent='analyze_orders', needsTools=False, analyses=[])
        with patch.object(agent, '_create_plan', AsyncMock(return_value=plan)):
            reply, _ = await agent.chat('synthetic-owner', '分析我的账单')
        self.assertIn('未得到可核算', reply)
