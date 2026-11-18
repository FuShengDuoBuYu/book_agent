import unittest
from datetime import date
from unittest.mock import AsyncMock

from pydantic import ValidationError

from app.agent.evidence_pipeline import collect_analysis_results
from app.agent.evidence_report import build_facts, validate_draft
from app.schemas.plan import AgentPlan, AnalysisRequest
from app.schemas.report import ReportDraft
from tests.test_aggregate_analysis import row, period


class AggregatePipelineTests(unittest.TestCase):
    def test_directions_in_old_snapshots_come_from_the_calculation(self):
        for tool, evidence, expected in [
            ('trends', {}, 'all'), ('changes', {}, 'all'), ('cashflow', {}, 'all'),
            ('income_stability', {}, 'income'), ('aggregate', {'metric': 'expense'}, 'expense'),
            ('sources', {'catalogMetric': 'expense'}, 'expense'),
        ]:
            with self.subTest(tool=tool):
                fact, = build_facts([{'tool': tool, 'metric': 'net',
                                      'evidence': {'sufficientData': False, **evidence}}])
                self.assertEqual(fact['binding']['metric'], expected)

    def test_source_and_member_bindings_follow_recorded_direction_even_for_default_income_request(self):
        plan = AgentPlan.model_validate({'toolCalls': [{'id': 'ledger', 'toolName': 'search_family_orders'}],
                                        'analyses': [{'tool': 'sources'}]})
        records = [row('2026-10-03', 1000, '工资'), row('2026-10-04', -200, '房租')]
        result, = collect_analysis_results(plan, {'家庭版': [(plan.toolCalls[0], records)]}, date(2026, 11, 19))
        self.assertEqual(result['metric'], 'all')
        self.assertEqual(result['requestedMetric'], 'income')
        facts = build_facts([dict(result, metric='income')])  # Older snapshots stored the requested direction.
        binds = [f['binding'] for f in facts]
        self.assertEqual({(b['member'], b['metric'], b.get('source')) for b in binds},
                         {('春枝', 'income', None), ('春枝', 'expense', None),
                          ('春枝', 'income', '工资'), ('春枝', 'expense', '房租')})

    def test_budget_binding_uses_actual_month_and_not_history_window(self):
        plan = AgentPlan.model_validate({'toolCalls': [{'id': 'ledger', 'toolName': 'search_personal_orders'}],
            'analysisWindow': {'startDate': '2026-10-01', 'endDate': '2026-10-31'},
            'analyses': [{'tool': 'budget', 'budgetAmount': 900, 'targetMonth': '2026-11'}]})
        records = [row('2026-10-03', -600, '房租'), row('2026-11-04', -200, '房租')]
        results = collect_analysis_results(plan, {'个人版': [(plan.toolCalls[0], records)]}, date(2026, 11, 19))
        budget = results[0]
        self.assertEqual(budget['evidence']['spent'], 200)
        self.assertIsNone(budget['historyWindow'])
        fact, = build_facts([dict(budget, metric='income', historyWindow=plan.analysisWindow.model_dump(mode='json'))])
        self.assertNotIn('historyWindow', fact['binding'])
        self.assertEqual((fact['binding']['metric'], fact['binding']['month']), ('expense', '2026-11'))

    def test_forecast_and_projection_bind_future_dates_separately_from_input_history(self):
        window = {'startDate': '2026-08-01', 'endDate': '2026-10-31'}
        plan = AgentPlan.model_validate({'toolCalls': [{'id': 'ledger', 'toolName': 'search_personal_orders'}],
            'analysisWindow': window, 'analyses': [
                {'tool': 'forecast', 'targetMonth': '2027-01', 'metric': 'expense'},
                {'tool': 'projection', 'targetMonth': '2027-01', 'months': 3}]})
        records = [row(f'2026-{m:02}-03', n, kind) for m in (8, 9, 10) for n, kind in [(1000, '工资'), (-200, '房租')]]
        results = collect_analysis_results(plan, {'个人版': [(plan.toolCalls[0], records)]}, date(2026, 11, 19))
        forecast, projection = [build_facts([dict(result, metric='income')])[0]['binding'] for result in results[:2]]
        self.assertEqual((forecast['historyWindow'], forecast['targetMonth'], forecast['metric']), (window, '2027-01', 'expense'))
        self.assertEqual((projection['historyWindow'], projection['startMonth'], projection['endMonth'],
                          projection['months'], projection['metric']), (window, '2027-01', '2027-03', 3, 'all'))

    def test_plan_validates_bounded_aggregate_arguments_and_forecast_direction(self):
        with self.assertRaises(ValidationError):
            AnalysisRequest.model_validate({'tool': 'aggregate', 'aggregate': {'groupBy': ['sql']}})
        with self.assertRaises(ValidationError):
            AnalysisRequest(tool='forecast', metric='all')
        task = AnalysisRequest.model_validate({'tool': 'aggregate', 'metric': 'all',
            'aggregate': {'groupBy': ['member', 'category']}})
        self.assertEqual(task.aggregate.groupBy, ['member', 'category'])

    def test_unused_scenario_fields_cannot_invalidate_historical_analysis(self):
        task = AnalysisRequest.model_validate({'tool': 'aggregate', 'metric': 'expense',
            'aggregate': {'groupBy': ['category']}, 'changes': [{'metric': 'expense', 'source': '房租'}]})
        self.assertEqual(task.changes, [])
        with self.assertRaises(ValidationError):
            AnalysisRequest.model_validate({'tool': 'scenario', 'changes': [{'metric': 'expense', 'source': '房租'}]})
        with self.assertRaises(ValidationError):
            AnalysisRequest.model_validate({'tool': 'aggregate', 'groupBy': ['category']})

    def test_descriptive_referent_overflow_is_bounded_without_discarding_tools(self):
        plan = AgentPlan.model_validate({'understanding': {'referents': ['记录范围'] * 9},
                                        'analyses': [{'tool': 'aggregate'}]})
        self.assertEqual(len(plan.understanding.referents), 8)
        self.assertEqual(plan.analyses[0].tool, 'aggregate')

    def test_query_bound_filters_periods_and_computed_values_reach_report(self):
        plan = AgentPlan.model_validate({'toolCalls': [{'id': 'family', 'toolName': 'search_family_orders'}],
            'output': {'table': True, 'metric': 'expense'}, 'analyses': [{'tool': 'aggregate',
                'queryIds': ['family'], 'metric': 'expense', 'aggregate': {'groupBy': ['member', 'category'],
                    'periods': [period('九月', '2026-09-01', '2026-09-30'),
                                period('十月', '2026-10-01', '2026-10-31')],
                    'filters': {'excludeCategories': ['房租']}}}]})
        records = [row('2026-09-05', -10, '餐饮'), row('2026-10-05', -30, '餐饮'),
                   row('2026-10-05', -900, '房租')]
        data = collect_analysis_results(plan, {'家庭版': [(plan.toolCalls[0], records)]}, date(2026, 11, 19))
        evidence = next(r['evidence'] for r in data if r['tool'] == 'aggregate')
        self.assertEqual(evidence['totals']['expense'], 40)
        self.assertEqual(evidence['comparisons'][0]['change'], 20)
        facts = build_facts([data[0]])
        text = '\n'.join(f['text'] for f in facts)
        self.assertIn('餐饮', text)
        self.assertIn('房租', text)
        self.assertIn('20', text)
        draft = ReportDraft.model_validate({'sections': [{'title': '区间比较',
            'factIds': [f['id'] for f in facts[offset:offset + 8]],
            'analysis': '餐饮已记支出增加20元；房租已按要求排除。' if offset == 0 else ''}
            for offset in range(0, len(facts), 8)]})
        self.assertFalse(validate_draft(draft, facts))

    def test_count_and_mean_changes_are_reported_as_ledger_statistics(self):
        from app.tools.financial_analysis import run_analysis
        records = [row('2026-09-05', -10, '票券'), row('2026-10-05', -10, '票券'),
                   row('2026-10-06', -30, '票券')]
        result = run_analysis('changes', records, as_of=date(2026, 11, 19),
                              window_start=date(2026, 9, 1), window_end=date(2026, 10, 31))
        facts = build_facts([{'tool': 'changes', 'evidence': result}])
        text = '\n'.join(f['text'] for f in facts)
        self.assertIn('笔数', text)
        self.assertIn('每笔', text)
        self.assertIn('单价', text)

    def test_directional_facts_do_not_call_filtered_income_or_net_a_ledger_total(self):
        from app.tools.financial_analysis import run_analysis
        data = run_analysis('aggregate', [row('2026-10-05', 500, '收入'), row('2026-10-06', -70, '餐饮')],
                            as_of=date(2026, 11, 19), metric='expense', aggregate={'groupBy': ['category']})
        facts = build_facts([{'tool': 'aggregate', 'evidence': data}])
        text = '\n'.join(f['text'] for f in facts)
        self.assertNotIn('收入 0 元', text)
        self.assertNotIn('净额 -70 元', text)
        self.assertIn('支出 70 元', text)

    def test_pipeline_marks_requested_periods_outside_actual_query_as_partial(self):
        plan = AgentPlan.model_validate({'toolCalls': [{'id': 'ledger', 'toolName': 'search_personal_orders',
                'args': {'year': 2026, 'month': 10}}], 'analyses': [{'tool': 'aggregate', 'metric': 'expense',
                    'aggregate': {'periods': [period('两月', '2026-09-01', '2026-10-31')]}}]})
        results = collect_analysis_results(plan, {'个人版': [(plan.toolCalls[0], [row('2026-10-05', -70, '餐饮')])]}, date(2026, 11, 19))
        data = results[0]['evidence']
        self.assertEqual(data['coverage'][0]['queryCoverage'], 'partial')
        self.assertEqual(data['coverage'][0]['effectiveStartDate'], '2026-10-01')
        text = '\n'.join(f['text'] for f in build_facts([results[0]]))
        self.assertIn('部分', text)

    def test_explicit_aggregate_window_respects_query_expansion_limit(self):
        from datetime import datetime
        from app.agent.planner import QueryPlanner
        plan = AgentPlan.model_validate({'analyses': [{'tool': 'aggregate'}],
            'analysisWindow': {'startDate': '2023-01-01', 'endDate': '2025-12-31'}})
        with self.assertRaises(ValueError):
            QueryPlanner.__new__(QueryPlanner)._normalize_plan(plan, datetime(2026, 11, 19), '按类别汇总这三年')

    def test_categorical_chart_does_not_materialize_unrelated_monthly_trends(self):
        from datetime import datetime
        from app.agent.planner import QueryPlanner
        plan = AgentPlan.model_validate({'analyses': [{'tool': 'aggregate', 'metric': 'expense',
            'aggregate': {'groupBy': ['weekday']}}], 'output': {'chart': 'bar', 'metric': 'expense'}})
        QueryPlanner.__new__(QueryPlanner)._normalize_plan(plan, datetime(2026, 11, 19), '按星期画图')
        self.assertNotIn('trends', [task.tool for task in plan.analyses])

    def test_declared_omission_units_are_checked_against_typed_facts(self):
        from app.agent.report_values import numerical_issues
        facts = [{'id': 'f1', 'values': [{'label': '省略分组', 'value': 4, 'unit': '个'},
                                       {'label': '省略比较', 'value': 2, 'unit': '项'}]}]
        self.assertFalse(numerical_issues('另有4个分组、2项比较未展示。', ['f1'], facts))
        self.assertTrue(numerical_issues('另有5个分组。', ['f1'], facts))

    def test_inline_internal_fact_markers_are_cleaned_without_erasing_quantities(self):
        from app.agent.evidence_report import prepare_draft
        facts = [{'id': 'f1', 'text': '计算收入100元', 'required': True,
                  'values': [{'label': '收入', 'value': 100, 'unit': '元'}]}]
        draft = ReportDraft.model_validate({'sections': [{'title': '来源', 'factIds': ['f1'],
            'analysis': '这笔收入（f1）为100元，后续是否延续需确认。'}]})
        prepared, _ = prepare_draft(draft, facts)
        self.assertNotIn('f1', prepared.sections[0].analysis)
        self.assertIn('100元', prepared.sections[0].analysis)


class AggregateInvestigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_refiner_keeps_distinct_filter_analyses_and_rejects_foreign_query(self):
        from app.agent.investigation import EvidenceInvestigator, InvestigationDecision
        plan = AgentPlan.model_validate({'toolCalls': [{'id': 'ledger', 'toolName': 'search_personal_orders'}],
            'analyses': [{'tool': 'aggregate', 'queryIds': ['ledger'], 'metric': 'expense',
                          'aggregate': {'groupBy': ['category']}}]})
        investigator = EvidenceInvestigator()
        investigator._invoke = AsyncMock(return_value=InvestigationDecision.model_validate({
            'additionalAnalyses': [
                {'tool': 'aggregate', 'queryIds': ['ledger'], 'metric': 'expense',
                 'aggregate': {'groupBy': ['weekday']}},
                {'tool': 'aggregate', 'queryIds': ['foreign'], 'metric': 'income'}]}))
        decision, audit = await investigator.refine('周末消费是否集中？', '', plan, [])
        self.assertEqual(len(decision.additionalAnalyses), 1)
        self.assertEqual(decision.additionalAnalyses[0].aggregate.groupBy, ['weekday'])
        self.assertTrue(audit['issues'])
