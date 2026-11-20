import unittest
from datetime import date

from app.agent.analysis_findings import build_analysis_findings
from app.agent.evidence_report import build_facts
from app.tools.financial_analysis import run_analysis
from tests.test_decision_analysis import ledger


class StatisticalBasisTests(unittest.TestCase):
    def test_income_stability_exposes_excluded_inflows_and_denominator(self):
        rows = ledger() + [{"year": 2026, "month": 10, "day": 10, "money": 8000,
                            "costType": "其他", "orderRemark": "旧设备出售"}]
        evidence = run_analysis('income_stability', rows, as_of=date(2026, 11, 20))
        self.assertEqual(evidence['incomeMin'], 6600)
        self.assertEqual(evidence['incomeMax'], 6600)
        self.assertEqual(evidence.get('incomeBasis'), 'complete_months_excluding_identified_nonrecurring')
        self.assertEqual(evidence.get('includedIncomeTotal'), 39600)
        self.assertEqual(evidence.get('excludedNonrecurringIncomeCount'), 1)
        self.assertEqual(evidence.get('excludedNonrecurringIncomeTotal'), 8000)
        result = {'tool': 'income_stability', 'mode': '个人版', 'evidence': evidence}
        facts = build_facts([result])
        headline = next(f for f in facts if f['required'])
        self.assertIn('剔除', headline['text'])
        self.assertIn('2026-05', headline['text'])
        self.assertIn('2026-10', headline['text'])
        self.assertIn('非持续', headline['text'])
        self.assertEqual(headline['binding'].get('incomeBasis'), evidence['incomeBasis'])
        share = next(f for f in facts if 'sharePercent' in {v['label'] for v in f['values']})
        self.assertIn('纳入收入', share['text'])
        self.assertIn('39,600', share['text'])
        findings = build_analysis_findings([result])['g1']
        text = ''.join(findings['observations'] + findings['limitations'])
        self.assertIn('剔除', text)
        self.assertIn('非持续', text)
        self.assertNotIn('观察到的完整月份收入相同', text)

    def test_cashflow_source_share_names_its_own_window_and_denominator(self):
        rows = ledger()
        evidence = run_analysis('cashflow', rows, as_of=date(2026, 11, 20),
                                window_start=date(2026, 5, 1), window_end=date(2026, 11, 20))
        result = {'tool': 'cashflow', 'mode': '个人版',
                  'historyWindow': {'startDate': '2026-05-01', 'endDate': '2026-11-20'},
                  'evidence': evidence}
        facts = build_facts([result])
        share = next(f for f in facts if 'sharePercent' in {v['label'] for v in f['values']})
        self.assertIn('2026-05-01 至 2026-11-20', share['text'])
        self.assertIn('39,600', share['text'])
        self.assertNotIn('查询期收入', share['text'])
        self.assertTrue(any('2026-05' in f['text'] and '2026-10' in f['text'] and f['required'] for f in facts))
        self.assertIn('历史窗口收入合计', {v['label'] for v in share['values']})
        findings = build_analysis_findings([result])['g1']
        self.assertIn('本次现金流取数窗口', ''.join(findings['observations']))

    def test_zero_income_cashflow_does_not_render_none_as_a_percentage(self):
        rows = [r for r in ledger() if r['money'] < 0]
        evidence = run_analysis('cashflow', rows, as_of=date(2026, 11, 20))
        self.assertNotIn('None%', evidence['computedConclusion'])
        self.assertIn('结余率不适用', evidence['computedConclusion'])
    def test_cashflow_uses_actual_query_intervals_even_without_analysis_window(self):
        from app.agent.evidence_pipeline import collect_analysis_results
        from app.schemas.plan import AgentPlan
        plan = AgentPlan(toolCalls=[{'id': 'may', 'toolName': 'search_personal_orders', 'args': {'year': 2026, 'month': 5}},
                                    {'id': 'july', 'toolName': 'search_personal_orders', 'args': {'year': 2026, 'month': 7}},
                                    {'id': 'nov', 'toolName': 'search_personal_orders', 'args': {'year': 2026, 'month': 11}}],
                         analyses=[{'tool': 'cashflow'}])
        results = collect_analysis_results(plan, {'个人版': [(c, [r for r in ledger() if r['month'] == c.args.month]) for c in plan.toolCalls]}, date(2026, 11, 20))
        evidence = results[0]['evidence']
        self.assertEqual(evidence.get('queryIntervals'), [
            {'startDate': '2026-05-01', 'endDate': '2026-05-31'},
            {'startDate': '2026-07-01', 'endDate': '2026-07-31'},
            {'startDate': '2026-11-01', 'endDate': '2026-11-20'}])
        source = next(f for f in build_facts(results) if 'sharePercent' in {v['label'] for v in f['values']})
        self.assertIn('2026-05-01 至 2026-05-31', source['text'])
        self.assertIn('2026-07-01 至 2026-07-31', source['text'])
        self.assertIn('2026-11-01 至 2026-11-20', source['text'])
        self.assertNotIn('未提供', source['text'])
        self.assertEqual(evidence['incomeTotal'], 13200)
        self.assertEqual(evidence['incomeSources'][0]['amount'], 10000)
        self.assertEqual(evidence['incomeSources'][0]['sharePercent'], 75.76)
        self.assertIn('13,200', source['text'])

    def test_cashflow_small_positive_income_is_not_treated_as_zero_after_monthly_rounding(self):
        rows = [{'year': 2026, 'month': month, 'day': 4, 'money': value,
                 'costType': '测试', 'orderRemark': ''} for month, value in [(8, .01), (9, -10), (10, -10)]]
        evidence = run_analysis('cashflow', rows, as_of=date(2026, 11, 20))
        self.assertEqual(evidence['completeMonthAverage']['netIncomeRatioPercent'], -199900)
        self.assertNotIn('收入为零', evidence['computedConclusion'])

    def test_scenario_without_income_does_not_render_none_percentage(self):
        evidence = run_analysis('scenario', ledger(), as_of=date(2026, 11, 20),
                                changes=[{'metric': 'income', 'percent': -100}])
        self.assertNotIn('None%', evidence['computedConclusion'])
        self.assertIn('结余率不适用', evidence['computedConclusion'])
