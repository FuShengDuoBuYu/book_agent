import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent.intent_resolution import IntentResolver


class IntentResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_execution_plan_uses_resolved_goal_with_bounded_history(self):
        from app.agent.planner import QueryPlanner
        from app.schemas.plan import IntentUnderstanding
        goal = IntentUnderstanding(objective='沿用这两个月，只统计展览的星期分布',
            referents=['原范围2026-09至2026-10'], requestedAnswers=['星期金额及笔数'], evidenceNeeds=['aggregate'])
        draft = {'analyses': [{'tool': 'aggregate', 'metric': 'expense', 'aggregate': {
            'groupBy': ['weekday'], 'filters': {'includeCategories': ['展览']}}}]}
        planner = QueryPlanner()
        planner.chain = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps(draft))))
        planner.reviewer = None
        planner.intent_resolver = SimpleNamespace(resolve=AsyncMock(return_value=(goal, {'status': 'resolved'})))
        result = await planner.create_plan('沿用这两个月，只看展览，按星期统计', '很长的旧回答' * 4000)
        self.assertEqual(result.understanding, goal)
        model_history = planner.chain.ainvoke.call_args.args[0]['history']
        self.assertIn(goal.objective, model_history)
        self.assertLess(len(model_history), 8000)
        self.assertTrue(any(row.get('type') == 'intent_resolution' for row in result._planning_issues))

    async def test_understanding_has_no_execution_or_invented_ledger_facts(self):
        understanding = {'objective': '沿用原范围，排除两类后比较月份支出',
            'referents': ['上上个月2026-09，上个月2026-10', '排除房租、交通'],
            'requestedAnswers': ['按类别金额、笔数、均额及变化'], 'evidenceNeeds': ['aggregate'],
            'unknowns': []}
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(
            content=json.dumps(understanding, ensure_ascii=False), response_metadata={'prompt_eval_count': 900, 'eval_count': 400},
            additional_kwargs={'reasoning_content': 'private reasoning must not be persisted'})))
        with patch('app.agent.intent_resolution.get_structured_model', return_value=model) as factory:
            result, audit = await IntentResolver().resolve('上上月和上月排除房租交通按类别对照', '',
                date(2026, 11, 19), '个人版', {})
        self.assertEqual(result.model_dump(), understanding)
        self.assertEqual(audit['status'], 'resolved')
        self.assertNotIn('reasoning_content', str(audit))
        self.assertNotIn('private reasoning', str(audit))
        self.assertEqual(factory.call_args.kwargs, {'reasoning': False, 'num_predict': 2048})
        payload = json.loads(model.ainvoke.call_args.args[0][1][1])
        self.assertEqual(payload['today'], '2026-11-19')

    async def test_unresolved_intent_is_recorded_without_guessing_financial_conditions(self):
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content='',
            response_metadata={'done_reason': 'length', 'eval_count': 2048})))
        with patch('app.agent.intent_resolution.get_structured_model', return_value=model):
            result, audit = await IntentResolver().resolve('把刚才的条件恢复', '历史', date(2026, 11, 19),
                '个人版', {'months': 3, 'startingBalance': 10000})
        self.assertIsNone(result)
        self.assertEqual(audit['status'], 'unavailable')
        self.assertEqual(audit['doneReason'], 'length')
        self.assertNotIn('startingBalance', audit)

    async def test_history_is_budgeted_but_question_and_conditions_survive(self):
        response = SimpleNamespace(content='{"objective":"恢复原假设","referents":[],"requestedAnswers":[],"evidenceNeeds":[],"unknowns":[]}', response_metadata={})
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=response))
        conditions = {'months': 3, 'changes': [{'metric': 'income', 'percent': -50, 'source': '稿费'}]}
        with patch('app.agent.intent_resolution.get_structured_model', return_value=model):
            _, audit = await IntentResolver().resolve('不再假设稿费减半', '过长历史' * 5000, date(2026, 11, 19), '个人版', conditions)
        payload = json.loads(model.ainvoke.call_args.args[0][1][1])
        self.assertEqual(payload['question'], '不再假设稿费减半')
        self.assertEqual(payload['userConditions'], conditions)
        self.assertLessEqual(audit['contextBudget']['estimatedPromptTokens'], audit['contextBudget']['promptBudgetTokens'])
