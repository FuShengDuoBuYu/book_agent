import json
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent.evidence_pipeline import collect_analysis_results
from app.agent.planner import QueryPlanner
from app.schemas.plan import AgentPlan, ToolCall


class PlanningInheritanceTests(unittest.IsolatedAsyncioTestCase):
    async def planned(self, draft, previous, message='把刚才的结果展开'):
        planner = QueryPlanner.__new__(QueryPlanner)
        planner.chain = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps(draft))))
        with patch('app.agent.planner.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 19)
            return await planner.create_plan(message, '', '个人版', previous)

    async def test_required_forecast_materializes_before_target_inheritance(self):
        previous = {'targetMonth': '2027-01', 'scope': [['search_personal_orders', '不限', '']]}
        plan = await self.planned({'followUpOfPreviousQuestion': True,
            'understanding': {'evidenceNeeds': ['sources', 'forecast']},
            'analyses': [{'tool': 'sources'}]}, previous)
        forecast = next(task for task in plan.analyses if task.tool == 'forecast')
        self.assertEqual(forecast.targetMonth, '2027-01')
        self.assertEqual(plan._analysis_context['targetMonth'], '2027-01')

    async def test_descriptive_intent_tags_do_not_discard_an_executable_plan(self):
        plan = await self.planned({'understanding': {'objective': '合并项目工序后预测收入',
            'evidenceNeeds': ['income_sources', 'source_mapping']},
            'analyses': [{'tool': 'forecast', 'metric': 'income', 'targetMonth': '2027-01'}, {'tool': 'sources'}]}, {},
            '预测2027年1月收入，同一项目合并')
        self.assertEqual(plan.intent, 'analyze_orders')
        self.assertEqual({task.tool for task in plan.analyses}, {'forecast', 'sources'})

    async def test_remark_subtask_does_not_erase_the_active_analysis(self):
        previous = {'targetMonth': '2027-01', 'scope': [['search_personal_orders', '不限', '']],
            'activeAnalyses': [{'tool': 'forecast', 'metric': 'income', 'targetMonth': '2027-01', 'queryIds': ['ledger']}],
            'conversation': {'analyses': [{'tool': 'sources'}],
                'queries': [{'id': 'ledger', 'toolName': 'search_personal_orders', 'args': {}}]}}
        plan = await self.planned({'followUpOfPreviousQuestion': True,
            'understanding': {'objective': '解释刚才预测月份的参照与计算方式', 'evidenceNeeds': ['sources', 'remarks']},
            'analyses': [{'tool': 'sources'}]}, previous)
        self.assertEqual(next(task.targetMonth for task in plan.analyses if task.tool == 'forecast'), '2027-01')

    async def test_explicit_new_history_window_does_not_replay_an_old_forecast(self):
        previous = {'targetMonth': '2027-01', 'scope': [['search_personal_orders', '不限', '']],
            'activeAnalyses': [{'tool': 'forecast', 'targetMonth': '2027-01'}],
            'conversation': {'analyses': [{'tool': 'forecast'}], 'analysisWindow': {'startDate': '2026-08-01', 'endDate': '2026-10-31'},
                'queries': [{'id': 'ledger', 'toolName': 'search_personal_orders', 'args': {}}]}}
        plan = await self.planned({'followUpOfPreviousQuestion': True,
            'analysisWindow': {'startDate': '2026-10-01', 'endDate': '2026-10-31'},
            'analyses': [{'tool': 'sources'}]}, previous, '只看上个月的入账来源')
        self.assertNotIn('forecast', {task.tool for task in plan.analyses})

    async def test_terse_followup_restores_the_referenced_family_query(self):
        previous = {'scope': [['search_family_orders', '不限', '']], 'startingBalance': 1000,
            'purchaseAmount': 700, 'conversation': {'analyses': [{'tool': 'purchase', 'queryIds': ['family'],
                'startingBalance': 1000, 'purchaseAmount': 700}],
                'queries': [{'id': 'family', 'toolName': 'search_family_orders', 'args': {}}]}}
        plan = await self.planned({'followUpOfPreviousQuestion': True}, previous)
        self.assertEqual(plan.intent, 'analyze_orders')
        self.assertTrue(plan.toolCalls)
        self.assertEqual({call.toolName for call in plan.toolCalls}, {'search_family_orders'})
        self.assertEqual(plan.analyses[0].startingBalance, 1000)
        self.assertEqual(plan._analysis_context['scope'], previous['scope'])

    async def test_explicit_new_personal_scope_cannot_copy_family_balance(self):
        previous = {'scope': [['search_family_orders', '不限', '']], 'startingBalance': 1000, 'purchaseAmount': 700,
            'conversation': {'analyses': [{'tool': 'purchase', 'startingBalance': 1000, 'purchaseAmount': 700}]}}
        plan = await self.planned({'followUpOfPreviousQuestion': True,
            'toolCalls': [{'id': 'personal', 'toolName': 'search_personal_orders'}],
            'analyses': [{'tool': 'purchase', 'purchaseAmount': 700}]}, previous, '只用我个人的情况判断')
        self.assertIsNone(plan.analyses[0].startingBalance)

    async def test_budget_and_forecast_have_distinct_target_months(self):
        for budget_month in (None, '2026-11'):
            plan = await self.planned({'analyses': [
                {'tool': 'budget', 'budgetAmount': 4000, 'targetMonth': budget_month},
                {'tool': 'forecast', 'metric': 'income', 'targetMonth': '2026-12'}]}, {},
                '本月预算还剩多少，下月收入多少？')
            self.assertEqual({task.tool: task.targetMonth for task in plan.analyses},
                             {'budget': '2026-11', 'forecast': '2026-12'})


class HistoricalWindowTests(unittest.TestCase):
    def test_income_direction_is_not_an_exact_category_filter(self):
        planner = QueryPlanner.__new__(QueryPlanner)
        for value in ('income', 'Income', '收入', 'expense'):
            plan = AgentPlan.model_validate({'toolCalls': [{'id': 'all', 'toolName': 'search_family_orders',
                'args': {'cost_type': value}}], 'analyses': [{'tool': 'sources'}]})
            plan = planner._normalize_plan(plan, datetime(2026, 11, 19), '按家庭成员列收入')
            self.assertEqual({call.args.cost_type for call in plan.toolCalls}, {'不限'})

    def test_production_adapter_forwards_explicit_forecast_sample_window(self):
        plan = AgentPlan.model_validate({'analysisWindow': {'startDate': '2026-08-01', 'endDate': '2026-09-30'},
            'analyses': [{'tool': 'forecast'}]})
        rows = [{'year': 2026, 'month': month, 'day': 3, 'money': amount, 'costType': '工资', 'orderRemark': '工资'}
                for month, amount in ((8, 100), (9, 100), (10, 10000))]
        result = collect_analysis_results(plan, {'个人版': [(ToolCall(id='ledger', toolName='search_personal_orders'), rows)]}, date(2026, 11, 19))
        forecast = next(row['evidence'] for row in result if row['tool'] == 'forecast')
        self.assertEqual(forecast['estimate'], 100)
        self.assertEqual(forecast['baselineMonths'], ['2026-08', '2026-09'])
