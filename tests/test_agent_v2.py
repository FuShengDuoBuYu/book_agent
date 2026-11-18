import json
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent.book_agent import BookAgent
from app.agent.analysis_context import resolve_context
from app.agent.planner import QueryPlanner
from app.schemas.plan import AgentPlan


class ConversationContextTests(unittest.TestCase):
    def test_history_includes_the_answer_being_followed_up(self):
        agent = BookAgent.__new__(BookAgent)
        text = agent._format_history([
            {'role': 'user', 'content': '下个月预计收入？'},
            {'role': 'assistant', 'content': '十二月收入由实习与带球组成。'},
            {'role': 'user', 'content': '分别列出来'},
        ])
        self.assertIn('助手:', text)
        self.assertIn('实习与带球', text)

    def test_forecast_followup_keeps_target_without_borrowing_another_scope(self):
        first = AgentPlan.model_validate({'analyses': [{'tool': 'forecast', 'targetMonth': '2026-12'}]})
        previous = resolve_context(first, {})
        next_plan = AgentPlan.model_validate({'followUpOfPreviousQuestion': True, 'analyses': [{'tool': 'forecast'}]})
        resolve_context(next_plan, previous)
        self.assertEqual(next_plan.analyses[0].targetMonth, '2026-12')
        other = AgentPlan.model_validate({'followUpOfPreviousQuestion': True,
            'toolCalls': [{'id': 'family', 'toolName': 'search_family_orders'}], 'analyses': [{'tool': 'forecast'}]})
        resolve_context(other, previous, '家庭版')
        self.assertIsNone(other.analyses[0].targetMonth)

    def test_chart_contract_repairs_missing_trend_tool(self):
        plan = AgentPlan.model_validate({'intent': 'analyze_orders', 'needsTools': True,
            'output': {'chart': 'line', 'metric': 'income'}, 'analyses': [{'tool': 'cashflow'}]})
        normalized = QueryPlanner.__new__(QueryPlanner)._normalize_plan(plan, datetime(2026, 11, 18), '近六个月收入折线图')
        self.assertIn('trends', [task.tool for task in normalized.analyses])


class InvestigationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_merged_alias_is_grounded_before_initial_forecast_is_sent_to_investigation(self):
        from app.agent.investigation import InvestigationDecision
        from app.tools.source_analysis import observed_source_key
        from tests.test_decision_analysis import ledger
        rows = ledger()
        keys = sorted({observed_source_key(row) for row in rows if row['money'] > 0})
        plan = AgentPlan.model_validate({'analyses': [{'tool': 'forecast', 'metric': 'income'}],
            'toolCalls': [{'id': 'ledger', 'toolName': 'search_personal_orders'}],
            'sourceGroups': [{'label': '同一个人的所有进账', 'sourceKeys': keys}]})
        captured = []
        async def reflect(question, history, plan, catalogs, *, results=None):
            captured.extend(results or [])
            return InvestigationDecision(), {'status': 'refined', 'issues': []}
        agent = BookAgent.__new__(BookAgent)
        agent._execute_tool_call = AsyncMock(return_value={'ok': True, 'mode': '个人版', 'ordersInfo': rows})
        agent.investigator = SimpleNamespace(refine=reflect)
        await agent._execute_tool_calls('个人版', None, plan, question='按实际来源拆开预测')
        initial = next(row['evidence'] for row in captured if row['tool'] == 'forecast')
        self.assertEqual(sorted(row['estimate'] for row in initial['sourceForecasts']), [1600, 5000])
        self.assertEqual(plan.sourceGroups, [])

    async def test_supplemental_category_summary_keeps_primary_monthly_expense_chart(self):
        from app.tools.financial_analysis import run_analysis
        from tests.test_aggregate_analysis import row

        plan = AgentPlan.model_validate({'intent': 'analyze_orders', 'needsTools': True,
            'analyses': [{'tool': 'trends', 'metric': 'expense'}],
            'toolCalls': [{'id': 'ledger', 'toolName': 'search_personal_orders'}],
            'output': {'chart': 'line', 'metric': 'expense'},
            'understanding': {'evidenceNeeds': ['trends']}})
        today = date(2026, 11, 19)
        records = [row('2026-09-02', -40, '餐饮'), row('2026-10-03', -100, '交通')]
        results = [
            {'tool': 'trends', 'mode': '个人版', 'supplemental': False,
             'evidence': run_analysis('trends', records, as_of=today)},
            {'tool': 'aggregate', 'mode': '个人版', 'supplemental': True,
             'evidence': run_analysis('aggregate', records, as_of=today, metric='expense',
                                      aggregate={'groupBy': ['category']})},
        ]
        agent = BookAgent.__new__(BookAgent)
        agent.memory = AsyncMock()
        agent.memory.ensure_session.return_value = 'synthetic'
        agent.memory.get_recent_messages.return_value = []
        agent._create_plan = AsyncMock(return_value=plan)
        agent._execute_tool_calls = AsyncMock(return_value=json.dumps({'analysisResults': results}))
        agent.reporter = SimpleNamespace(answer=AsyncMock(return_value=('核算完成。', {})))

        async def immediate_progress(work, _):
            yield {'type': 'result', 'value': await work}

        with patch('app.agent.book_agent.with_progress', immediate_progress):
            events = [event async for event in agent.stream_chat('synthetic', '近两个月支出折线图')]
        self.assertEqual(events[-1]['type'], 'done')
        artifacts = next(event['artifacts'] for event in events if event['type'] == 'artifacts')
        charts = [item for item in artifacts if item['kind'] == 'line']
        self.assertEqual([item['xKey'] for item in charts], ['month'])
        self.assertEqual(charts[0]['series'], [{'key': 'expense', 'label': '支出'}])
        self.assertEqual([(item['month'], item['expense']) for item in charts[0]['rows']],
                         [('2026-09', 40), ('2026-10', 100)])
        supplementary_tables = [item for item in artifacts if item['kind'] == 'table']
        self.assertEqual({item['category'] for item in supplementary_tables[0]['rows']}, {'餐饮', '交通'})

    async def test_supplemental_income_reconciles_but_does_not_become_the_followup_goal(self):
        from app.agent.investigation import EvidenceInvestigator, InvestigationDecision
        from app.schemas.plan import AnalysisRequest
        from tests.test_aggregate_analysis import row

        agent = BookAgent.__new__(BookAgent)
        agent.investigator = EvidenceInvestigator()
        agent.investigator._invoke = AsyncMock(return_value=InvestigationDecision.model_validate({
            'additionalAnalyses': [{'tool': 'aggregate', 'metric': 'income', 'queryIds': ['ledger'],
                'aggregate': {'filters': {'includeSources': ['k1', 'k2'], 'excludeSources': ['k2']}}}]}))
        records = [row('2026-08-01', 150, '项目收入'), row('2026-08-02', 100, '实习收入')]
        agent._execute_tool_call = AsyncMock(return_value={'mode': '个人版', 'ok': True, 'ordersInfo': records})
        agent.memory = AsyncMock()
        plan = AgentPlan.model_validate({'analyses': [
            {'tool': 'forecast', 'queryIds': ['ledger'], 'metric': 'income', 'targetMonth': '2027-01'}],
            'toolCalls': [{'id': 'ledger', 'toolName': 'search_personal_orders'}]})
        plan._analysis_context = resolve_context(plan, {})
        data = json.loads(await agent._execute_tool_calls('个人版', None, plan, question='预测一月收入，并核对来源'))
        results = {result['tool']: result for result in data['analysisResults']}
        self.assertEqual(results['aggregate']['evidence']['totals']['income'], 150)
        self.assertTrue(results['aggregate']['supplemental'])
        self.assertTrue(results['sources']['supplemental'])
        self.assertFalse(results['forecast']['supplemental'])
        self.assertNotIn('_supplemental', AnalysisRequest.model_json_schema()['properties'])
        await agent._save_assistant_reply('test', 'test-session', '一月收入预测', plan, evidence=data)
        saved = agent.memory.add_message.await_args.kwargs['metadata']
        ctx = saved['analysisContext']
        self.assertEqual([t['tool'] for t in ctx['activeAnalyses']], ['forecast'])
        self.assertEqual([t['tool'] for t in ctx['conversation']['analyses']], ['forecast'])
        followup = AgentPlan(intent='analyze_orders', followUpOfPreviousQuestion=True)
        QueryPlanner.__new__(QueryPlanner)._inherit_conversation(followup, ctx)
        self.assertEqual([(t.tool, t.targetMonth) for t in followup.analyses], [('forecast', '2027-01')])

    async def test_reflection_sees_initial_calculation_and_recomputes_grouped_forecast(self):
        from zoneinfo import ZoneInfo
        from app.agent.investigation import InvestigationDecision
        from scripts.evaluate_intent import IntentLedger, STAGES
        today = datetime.now(ZoneInfo('Asia/Shanghai'))
        agent = BookAgent(IntentLedger(today.date()))
        plan = AgentPlan.model_validate({'analyses': [{'tool': 'forecast', 'metric': 'income'}]})
        plan = QueryPlanner.__new__(QueryPlanner)._normalize_plan(plan, today, '同一项目按一个来源预测')
        initial = []

        async def reflect(question, history, plan, catalogs, *, results=None):
            initial.extend(results or [])
            keys = [row['sourceKey'] for c in catalogs for row in c['sources']
                    if any(remark in STAGES for remark in row.get('remarks', []))]
            return InvestigationDecision.model_validate({'sourceGroups': [{'label': '星港项目', 'sourceKeys': keys}]}), {'status': 'refined'}

        agent.investigator.refine = reflect
        data = json.loads(await agent._execute_tool_calls('个人版', None, plan, question='按项目预测', history=''))
        self.assertTrue(any(row['tool'] == 'forecast' for row in initial), '调查阶段必须收到初次计算结果')
        first = next(row['evidence'] for row in initial if row['tool'] == 'forecast')
        final = next(row['evidence'] for row in data['analysisResults'] if row['tool'] == 'forecast')
        self.assertEqual(first['estimate'], 3200)
        self.assertEqual(final['estimate'], 3700)


if __name__ == '__main__':
    unittest.main()
