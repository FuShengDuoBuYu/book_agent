import unittest
from datetime import date

from app.agent.evidence_report import build_facts
from app.agent.report_values import value_catalog
from app.tools.financial_analysis import run_analysis
from tests.test_decision_analysis import ledger


class FactStatisticsTests(unittest.TestCase):
    def test_forecast_headline_binds_target_and_distinguishes_baseline_from_observation_months(self):
        result = run_analysis('forecast', ledger(), as_of=date(2026, 11, 19), target_month='2027-01')
        facts = build_facts([{'tool': 'forecast', 'evidence': result}])
        headline = facts[0]
        self.assertEqual(headline['binding']['targetMonth'], '2027-01')
        values = value_catalog([headline])
        self.assertEqual([r['value'] for r in values if r['label'] == '参考月份'], result['baselineMonths'])
        self.assertEqual([r['value'] for r in values if r['label'] == '历史观察月份'], result['observedMonths'])
        self.assertGreater(len(result['observedMonths']), len(result['baselineMonths']))
        self.assertTrue(all(r['factId'] == headline['id'] for r in values))

    def test_computed_month_and_duplicate_counts_have_typed_provenance(self):
        for tool in ('cashflow', 'income_stability', 'anomalies'):
            with self.subTest(tool=tool):
                evidence = run_analysis(tool, ledger(), as_of=date(2026, 11, 19))
                facts = build_facts([{'tool': tool, 'evidence': evidence}])
                if tool == 'anomalies':
                    expected = ('疑似重复组数', len(evidence['possibleDuplicates']), '组')
                else:
                    months = evidence['completeMonthAverage']['months'] if tool == 'cashflow' else evidence['observedMonths']
                    expected = ('完整月份数', len(months), '个月')
                self.assertIn(expected, [(v['label'], v['value'], v['unit']) for v in value_catalog(facts)])
