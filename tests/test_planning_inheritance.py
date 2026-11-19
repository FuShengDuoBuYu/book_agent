import json
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent.evidence_pipeline import collect_analysis_results
from app.agent.planner import QueryPlanner
from app.schemas.plan import AgentPlan, ToolCall


class PlanningInheritanceTests(unittest.IsolatedAsyncioTestCase):
    async def planned(self, draft, previous, message='把刚才的结果展开', book_mode='个人版'):
        planner = QueryPlanner.__new__(QueryPlanner)
        planner.chain = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps(draft))))
        with patch('app.agent.planner.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 19)
            return await planner.create_plan(message, '', book_mode, previous)

    async def test_family_member_request_corrects_personal_query_before_calendar_expansion(self):
        draft = {'intent': 'analyze_orders', 'analysisType': 'category_expense',
                 'toolCalls': [{'id': 'income', 'toolName': 'search_personal_orders'}],
                 'understanding': {'objective': '按家庭成员说明收入来源', 'evidenceNeeds': ['members', 'sources']}}
        question = '在我们家上一个完整月的收入里，谁从哪些地方收到了钱？即使备注相同，也请按人分清楚。'
        for mode in ['家庭版', '个人版']:
            with self.subTest(mode=mode):
                p = await self.planned(draft, {}, question, mode)
                self.assertEqual([(c.toolName, c.args.year, c.args.month, c.sourceIds) for c in p.toolCalls],
                                 [('search_family_orders', 2026, 10, ['income'])])
                self.assertEqual(p._analysis_context['scope'], [['search_family_orders', '不限', '']])

    async def test_current_ledger_and_filters_are_grounded_in_the_request_or_ui(self):
        tests = [('本月收入有哪些？', '家庭版', 'search_family_orders'),
                 ('我的收入来源有哪些？', '家庭版', 'search_family_orders'),
                 ('我个人在家里做饭花了多少？', '家庭版', 'search_personal_orders'),
                 ('在我们家的账本里只看我个人的收入，各月份分别统计。', '个人版', 'search_family_orders'),
                 ('对照个人账本与家庭用品的预算明细。', '个人版', 'search_personal_orders'),
                 ('上次查的是家庭账本，这次核对收入。', '个人版', 'search_personal_orders'),
                 ('只查个人账本里备注写着“家庭账本”的餐饮消费', '家庭版', 'search_personal_orders'),
                 ('不要查家庭账本，只看个人账本。', '家庭版', 'search_personal_orders')]
        for question, mode, tool in tests:
            with self.subTest(question=question):
                other = 'search_personal_orders' if tool == 'search_family_orders' else 'search_family_orders'
                draft = {'toolCalls': [{'id': 'q', 'toolName': other, 'args': {'cost_type': '餐饮', 'remark': '午饭'}}],
                         'analyses': [{'tool': 'sources', 'queryIds': ['q']}]}
                p = await self.planned(draft, {}, question, mode)
                self.assertEqual({(c.toolName, c.args.cost_type, c.args.remark, tuple(c.sourceIds)) for c in p.toolCalls},
                                 {(tool, '餐饮', '午饭', ('q',))})
                self.assertEqual(p.analyses[0].queryIds, ['q'])

    async def test_previous_ledger_cannot_override_current_ui_when_no_new_query_is_proposed(self):
        prior = {'scope': [['search_family_orders', '不限', '']], 'startingBalance': 1000,
                 'conversation': {'queries': [{'id': 'family', 'toolName': 'search_family_orders'}],
                                  'analyses': [{'tool': 'purchase', 'queryIds': ['family']}]}}
        p = await self.planned({'followUpOfPreviousQuestion': True, 'analyses': [{'tool': 'purchase'}]},
                               prior, '按相同的方法再判断能否购买。', '个人版')
        self.assertEqual({c.toolName for c in p.toolCalls}, {'search_personal_orders'})
        self.assertIsNone(p.analyses[0].startingBalance)

    async def test_mixed_ledgers_require_an_actual_request_for_both(self):
        queries = [{'id': 'p', 'toolName': 'search_personal_orders'}, {'id': 'f', 'toolName': 'search_family_orders'}]
        draft = {'toolCalls': queries, 'analyses': [{'tool': 'sources', 'queryIds': [key]} for key in ['p', 'f']]}
        for question, both in [('分别核对个人账本和家庭账本的收入来源。', True), ('核对收入来源。', False)]:
            with self.subTest(question=question):
                p = await self.planned(draft, {}, question, '家庭版')
                self.assertEqual({c.toolName for c in p.toolCalls}, {q['toolName'] for q in queries} if both else {'search_family_orders'})
                self.assertEqual({tuple(t.queryIds) for t in p.analyses}, {('p',), ('f',)} if both else {('f',)})

    async def test_one_ledger_cannot_stand_in_for_an_explicit_two_ledger_comparison(self):
        p = await self.planned({'toolCalls': [{'id': 'p', 'toolName': 'search_personal_orders'}],
                                'analyses': [{'tool': 'cashflow', 'queryIds': ['p']}]},
                               {}, '分别核对个人账本与家庭账本的本月收支。')
        self.assertFalse(p.toolCalls)
        self.assertTrue(any('两个账本' in issue.get('detail', '') for issue in p._planning_issues))

    async def test_overall_financial_diagnosis_has_a_cashflow_baseline_without_a_totals_tag(self):
        question = '综合看看我最近半年的财务情况，最值得注意的三个问题是什么？'
        draft = {'understanding': {'evidenceNeeds': ['changes', 'anomalies', 'sources']},
                 'analyses': [{'tool': 'aggregate'}, {'tool': 'trends'}]}
        plan = await self.planned(draft, {}, question)
        self.assertEqual([t.tool for t in plan.analyses].count('cashflow'), 1)
        self.assertEqual(plan.analysisWindow.startDate, date(2026, 5, 1))
        self.assertEqual(plan.analysisWindow.endDate, date(2026, 11, 19))
        for text, extra in [
            ('分析财务情况，只看收入来源。', {}),
            ('我的收入主要从哪里来？', {}),
            (question, {'toolCalls': [{'id': 'x', 'toolName': 'search_personal_orders', 'args': {'remark': '稿费'}}]}),
            (question, {'analyses': [{'tool': 'aggregate', 'aggregate': {'filters': {'includeCategories': ['餐饮']}}}]}),
            ('分别对照个人账本与家庭账本的财务状况。', {'toolCalls': [
                {'id': 'mine', 'toolName': 'search_personal_orders'}, {'id': 'home', 'toolName': 'search_family_orders'}]}),
            ('未来三个月的财务状况能存下多少？', {'analyses': [{'tool': 'projection', 'months': 3}]})]:
            with self.subTest(text=text, extra=extra):
                plan = await self.planned({**draft, **extra}, {}, text)
                self.assertTrue(plan.toolCalls)
                self.assertNotIn('cashflow', [t.tool for t in plan.analyses])

    async def test_semantic_review_corrects_tools_and_target_before_date_expansion(self):
        from app.agent.plan_review import PlanReviewDecision
        planner = QueryPlanner.__new__(QueryPlanner)
        draft = {'understanding': {'objective': '预测2027年1月收入'},
            'analyses': [{'tool': 'trends'}],
            'analysisWindow': {'startDate': '2026-11-01', 'endDate': '2026-11-19'}}
        planner.chain = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps(draft))))
        decision = PlanReviewDecision.model_validate({'adequate': False, 'issues': ['趋势工具不能预测未来'],
            'analyses': [{'tool': 'forecast', 'targetMonth': '2027-01'}],
            'analysisWindow': None, 'answerInstruction': '解释目标月的条件估计和来源。'})
        planner.reviewer = SimpleNamespace(review=AsyncMock(return_value=(decision, {'status': 'reviewed'})))
        with patch('app.agent.planner.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 19)
            plan = await planner.create_plan('预计2027年1月收入', '', '个人版', {})
        self.assertEqual([t.targetMonth for t in plan.analyses if t.tool == 'forecast'], ['2027-01'])
        self.assertIsNone(plan.analysisWindow)
        self.assertEqual([q.args.month for q in plan.toolCalls], [5, 6, 7, 8, 9, 10, 11])
        self.assertEqual(plan._semantic_review['status'], 'reviewed')

    async def test_adequate_review_preserves_correct_native_and_composite_plans(self):
        from app.agent.plan_review import PlanReviewDecision
        for native in (True, False):
            draft = ({'analysisType': 'highest_expense', 'toolCalls': [{'toolName': 'search_personal_orders',
                'args': {'year': 2026, 'month': 11, 'day': 18}}]} if native else {
                'analyses': [{'tool': 'forecast', 'targetMonth': '2027-01'}, {'tool': 'sources'}]})
            planner = QueryPlanner.__new__(QueryPlanner)
            planner.chain = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps(draft))))
            decision = PlanReviewDecision.model_validate({'adequate': True, 'issues': [],
                'analyses': [{'tool': 'cashflow' if native else 'sources'}],
                'analysisWindow': None, 'answerInstruction': '原计划已足够'})
            planner.reviewer = SimpleNamespace(review=AsyncMock(return_value=(decision, {'status': 'reviewed'})))
            with patch('app.agent.planner.datetime', wraps=datetime) as clock:
                clock.now.return_value = datetime(2026, 11, 19)
                plan = await planner.create_plan('昨天最大一笔消费' if native else '预计2027年1月收入及来源', '', '个人版', {})
            if native:
                self.assertEqual(len(plan.toolCalls), 1)
                self.assertEqual(plan.toolCalls[0].args.day, 18)
                self.assertEqual(plan.analyses, [])
            else:
                self.assertIn('forecast', [task.tool for task in plan.analyses])

    async def test_unproven_balance_does_not_execute_or_persist_when_review_unavailable(self):
        planner = QueryPlanner.__new__(QueryPlanner)
        draft = {'analyses': [{'tool': 'purchase', 'purchaseAmount': 700, 'startingBalance': 9999}]}
        planner.chain = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps(draft))))
        planner.reviewer = SimpleNamespace(review=AsyncMock(return_value=(None, {'status': 'unavailable', 'error': 'TimeoutError'})))
        with patch('app.agent.planner.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 19)
            plan = await planner.create_plan('我没有提供余额，能花700元买扫描仪吗？', '', '个人版', {})
        self.assertIsNone(plan.analyses[0].startingBalance)
        self.assertIsNone(plan._analysis_context.get('startingBalance'))

    async def test_direction_aliases_are_canonical_before_context_resolution(self):
        first = await self.planned({'toolCalls': [{'id': 'ledger', 'toolName': 'search_personal_orders',
            'args': {'cost_type': 'income'}}], 'analyses': [{'tool': 'forecast', 'targetMonth': '2027-01'}]}, {}, '预测明年一月收入')
        self.assertEqual(first._analysis_context['scope'], [['search_personal_orders', '不限', '']])

    async def test_method_explanation_restores_primary_calculation_before_support_tools(self):
        previous = {'scope': [['search_personal_orders', '不限', '']], 'targetMonth': '2027-01',
            'activeHistoryWindow': None, 'activeAnalyses': [{'tool': 'forecast', 'targetMonth': '2027-01'}],
            'conversation': {'queries': [{'id': 'ledger', 'toolName': 'search_personal_orders'}],
                'analyses': [{'tool': 'sources'}], 'analysisWindow': None}}
        plan = await self.planned({'followUpOfPreviousQuestion': True,
            'understanding': {'objective': '解释刚才预测的参照月份与计算过程', 'evidenceNeeds': ['sources', 'trends']},
            'toolCalls': [{'id': 'ledger', 'toolName': 'search_personal_orders', 'args': {'cost_type': 'income'}}],
            'analyses': [{'tool': 'cashflow', 'targetMonth': '2027-01'}],
            'analysisWindow': {'startDate': '2026-10-01', 'endDate': '2026-10-31'}}, previous,
            '刚才预测那个月拿哪几个月作参照，怎么从分项算到总数？')
        self.assertIsNone(plan.analysisWindow)
        self.assertEqual([a.targetMonth for a in plan.analyses if a.tool == 'forecast'], ['2027-01'])

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

    async def test_complete_month_request_repairs_wrong_model_window_before_fetch(self):
        draft = {'analysisWindow': {'startDate': '2026-10-01', 'endDate': '2026-11-19'},
                 'analyses': [{'tool': 'changes'}]}
        for wording, months in [('最近两个完整月', 2), ('上3个完整自然月', 3), ('最近十二个完整月份', 12)]:
            with self.subTest(wording=wording):
                plan = await self.planned(draft, {}, f'对照我{wording}，哪项开销把总支出拉高了？')
                start_year, offset = divmod(2026 * 12 + 10 - months, 12)
                self.assertEqual(plan.analysisWindow.startDate, date(start_year, offset + 1, 1))
                self.assertEqual(plan.analysisWindow.endDate, date(2026, 10, 31))
                self.assertEqual(len(plan.toolCalls), months)
                self.assertEqual((plan.toolCalls[-1].args.year, plan.toolCalls[-1].args.month), (2026, 10))

    async def test_simple_complete_month_query_expands_wrong_single_month_lookup(self):
        plan = await self.planned({'analysisType': 'total_spending', 'toolCalls': [
            {'id': 'expenses', 'toolName': 'search_personal_orders', 'args': {'year': 2026, 'month': 11}}]},
            {}, '最近两个完整月总共花了多少？不含本月。')
        self.assertEqual([(q.args.year, q.args.month) for q in plan.toolCalls], [(2026, 9), (2026, 10)])
        self.assertEqual(plan.analysisWindow.endDate, date(2026, 10, 31))

    async def test_forecast_target_does_not_override_explicit_complete_sample_months(self):
        plan = await self.planned({'analysisWindow': {'startDate': '2026-10-01', 'endDate': '2026-11-19'},
            'analyses': [{'tool': 'forecast', 'targetMonth': '2027-01'}]}, {},
            '用最近两个完整月的记录，估计2027年1月收入')
        self.assertEqual([q.args.month for q in plan.toolCalls], [9, 10])
        self.assertEqual(plan.analyses[0].targetMonth, '2027-01')

    async def test_rolling_and_compound_periods_keep_model_window(self):
        draft = {'analysisWindow': {'startDate': '2026-09-20', 'endDate': '2026-11-19'},
                 'analyses': [{'tool': 'trends'}]}
        for question in ('最近两个月的收入', '比较最近两个完整月和本月至今',
                         '比较最近两个完整月与最近三个完整月', '截至2025年1月的上两个完整月'):
            with self.subTest(question=question):
                plan = await self.planned(draft, {}, question)
                self.assertEqual(plan.analysisWindow.model_dump(mode='json'), draft['analysisWindow'])

    async def test_historical_pivot_replaces_the_saved_forecast_referent(self):
        from app.agent.analysis_context import latest_context
        from app.agent.book_agent import BookAgent
        agent = BookAgent.__new__(BookAgent)
        agent.memory = AsyncMock()

        async def saved(plan):
            await agent._save_assistant_reply('synthetic', 'local', '已核算', plan)
            message = agent.memory.add_message.await_args.kwargs
            return latest_context(json.loads(json.dumps([message], ensure_ascii=False)))

        original = await self.planned({'analyses': [{'tool': 'forecast', 'targetMonth': '2027-01'}]}, {},
                                      '预计明年一月收入')
        for explicit_history in (True, False):
            with self.subTest(explicit_history=explicit_history):
                second_draft = {'followUpOfPreviousQuestion': True, 'analyses': [{'tool': 'sources'}]}
                if explicit_history:
                    second_draft['analysisWindow'] = {'startDate': '2026-10-01', 'endDate': '2026-10-31'}
                second = await self.planned(second_draft, await saved(original),
                                            '只看十月入账来源' if explicit_history else '列出刚才预测的来源')
                third = await self.planned({'followUpOfPreviousQuestion': True, 'analyses': [{'tool': 'sources'}]},
                                           await saved(second), '按刚才的口径列出每个来源的备注')
                forecasts = [task for task in third.analyses if task.tool == 'forecast']
                if explicit_history:
                    self.assertEqual(forecasts, [])
                    self.assertEqual(third.analysisWindow.model_dump(mode='json'), second_draft['analysisWindow'])
                    self.assertNotIn('targetMonth', third._analysis_context)
                else:
                    self.assertEqual([task.targetMonth for task in forecasts], ['2027-01'])

    async def test_aggregate_periods_fetch_the_enclosing_range_or_explicit_window(self):
        periods = [{'label': '甲', 'startDate': '2025-02-11', 'endDate': '2025-02-27'},
                   {'label': '乙', 'startDate': '2025-04-16', 'endDate': '2025-04-24'}]
        draft = {'analyses': [{'tool': 'aggregate', 'metric': 'expense', 'aggregate': {'periods': periods}}]}
        result = await self.planned(draft, {}, '比较这两个历史时段')
        self.assertEqual([(q.args.year, q.args.month) for q in result.toolCalls], [(2025, 2), (2025, 3), (2025, 4)])
        constrained = await self.planned({**draft, 'analysisWindow': {'startDate': '2025-04-01', 'endDate': '2025-04-30'}},
                                         {}, '只算四月范围内的记录')
        self.assertEqual([(q.args.year, q.args.month) for q in constrained.toolCalls], [(2025, 4)])
        periods[0]['startDate'] = '2023-01-01'
        too_long = await self.planned(draft, {}, '比较这些月份')
        self.assertFalse(too_long.toolCalls)
        self.assertTrue(any('24' in issue.get('detail', '') for issue in too_long._planning_issues))

    async def test_terse_followup_restores_the_referenced_family_query(self):
        previous = {'scope': [['search_family_orders', '不限', '']], 'startingBalance': 1000,
            'purchaseAmount': 700, 'conversation': {'analyses': [{'tool': 'purchase', 'queryIds': ['family'],
                'startingBalance': 1000, 'purchaseAmount': 700}],
                'queries': [{'id': 'family', 'toolName': 'search_family_orders', 'args': {}}]}}
        plan = await self.planned({'followUpOfPreviousQuestion': True}, previous, book_mode='家庭版')
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


class ForecastSampleProvenanceTests(unittest.TestCase):
    def test_referent_map_in_array_is_recovered_without_changing_executable_args(self):
        planner = QueryPlanner.__new__(QueryPlanner)
        raw = '''{"understanding":{"referents":["上个月":"2026-10","按人":"保留成员"]},
          "toolCalls":[{"id":"family","toolName":"search_family_orders"}],
          "analyses":[{"tool":"sources","queryIds":["family"]}]}'''
        data = planner._extract_json(raw)
        self.assertEqual(data['understanding']['referents'], ['上个月：2026-10', '按人：保留成员'])
        self.assertEqual(data['toolCalls'][0]['toolName'], 'search_family_orders')
        with self.assertRaises(json.JSONDecodeError):
            planner._extract_json('{"analyses":[{"tool":"scenario","changes":["expense":300]}]}')

    def test_explicit_monthly_improvement_is_a_goal_not_an_assumed_cut(self):
        planner = QueryPlanner.__new__(QueryPlanner)
        for raw in ('三百', '300', '一千二百', '两千五百元'):
            amount = {'三百': 300, '300': 300, '一千二百': 1200, '两千五百元': 2500}[raw]
            plan = AgentPlan.model_validate({'analyses': [{'tool': 'optimization', 'changes': [
                {'metric': 'expense', 'amount': -amount, 'label': '每月多留目标'}]}]})
            planner._normalize_explicit_conditions(plan, '房租保留，我想每月多留' + raw)
            self.assertEqual(plan.analyses[0].monthlyImprovement, amount)
            self.assertEqual(plan.analyses[0].changes, [])

    def test_ambiguous_or_negated_goals_do_not_become_known_conditions(self):
        planner = QueryPlanner.__new__(QueryPlanner)
        for text in ('不是每月多留三百', '每月多留三百还是每月多留五百？'):
            plan = AgentPlan.model_validate({'analyses': [{'tool': 'optimization'}]})
            planner._normalize_explicit_conditions(plan, text)
            self.assertIsNone(plan.analyses[0].monthlyImprovement)

    def test_undated_followup_cannot_replace_referents_sample_with_model_default(self):
        planner = QueryPlanner.__new__(QueryPlanner)
        previous = {'scope': [['search_personal_orders', '不限', '']], 'targetMonth': '2027-01',
            'activeAnalyses': [{'tool': 'forecast', 'queryIds': ['ledger'], 'targetMonth': '2027-01'}],
            'activeHistoryWindow': None, 'conversation': {'analysisWindow': None,
                'queries': [{'id': 'ledger', 'toolName': 'search_personal_orders'}],
                'analyses': [{'tool': 'forecast', 'queryIds': ['ledger'], 'targetMonth': '2027-01'}]}}
        plan = AgentPlan.model_validate({'followUpOfPreviousQuestion': True, 'analyses': [{'tool': 'sources'}],
            'analysisWindow': {'startDate': '2026-10-01', 'endDate': '2026-10-31'}})
        planner._preserve_referent_window(plan, previous, '刚才的原始备注列出来', datetime(2026, 11, 19))
        planner._inherit_conversation(plan, previous)
        self.assertIsNone(plan.analysisWindow)
        self.assertIn('forecast', [task.tool for task in plan.analyses])

    def test_future_target_in_question_does_not_authorize_invented_current_sample(self):
        planner = QueryPlanner.__new__(QueryPlanner)
        plan = AgentPlan.model_validate({'analyses': [{'tool': 'forecast', 'targetMonth': '2027-01'}],
            'analysisWindow': {'startDate': '2026-11-01', 'endDate': '2026-12-31'}})
        result = planner._normalize_plan(plan, datetime(2026, 11, 19), '预计2027年1月能进账多少？')
        self.assertIsNone(result.analysisWindow)
        self.assertEqual([c.args.month for c in result.toolCalls], [5, 6, 7, 8, 9, 10, 11])
        self.assertEqual(result.analyses[0].targetMonth, '2027-01')

    def test_explicit_current_sample_is_respected_even_with_future_target(self):
        planner = QueryPlanner.__new__(QueryPlanner)
        plan = AgentPlan.model_validate({'analyses': [{'tool': 'forecast', 'targetMonth': '2027-01'}],
            'analysisWindow': {'startDate': '2026-11-01', 'endDate': '2026-11-19'}})
        result = planner._normalize_plan(plan, datetime(2026, 11, 19), '仅用本月已记数据，预计2027年1月能进账多少？')
        self.assertIsNotNone(result.analysisWindow)
        self.assertEqual([c.args.month for c in result.toolCalls], [11])
