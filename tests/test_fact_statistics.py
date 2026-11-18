import unittest
from datetime import date

from app.agent.evidence_report import build_facts
from app.agent.report_values import numerical_issues
from app.tools.financial_analysis import run_analysis
from tests.test_decision_analysis import ledger


class FactStatisticsTests(unittest.TestCase):
    def test_computed_month_and_duplicate_counts_have_typed_provenance(self):
        for tool in ('cashflow', 'income_stability', 'anomalies'):
            evidence = run_analysis(tool, ledger(), as_of=date(2026, 11, 19))
            facts = build_facts([{'tool': tool, 'evidence': evidence}])
            if tool == 'anomalies':
                text = f"规则发现{len(evidence['possibleDuplicates'])}组疑似重复。"
            else:
                months = (evidence['completeMonthAverage']['months'] if tool == 'cashflow' else evidence['observedMonths'])
                text = f"观察到{len(months)}个完整月份。"
            self.assertFalse(numerical_issues(text, [f['id'] for f in facts], facts), tool)
