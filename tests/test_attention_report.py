import unittest
from datetime import date
from unittest.mock import AsyncMock

from app.agent.evidence_report import EvidenceReporter
from app.tools.financial_analysis import run_analysis
from tests.test_decision_analysis import ledger


class AttentionReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_requested_issues_are_synthesized_even_if_outline_times_out(self):
        records = [{**row, 'bankName': '现金'} for row in ledger()]
        records.append({'year': 2026, 'month': 10, 'day': 8, 'money': -900,
                        'costType': '医疗', 'orderRemark': '门诊', 'bankName': '现金'})
        results = [{'tool': tool, 'mode': '个人版', 'evidence': run_analysis(
            tool, records, as_of=date(2026, 11, 19))}
            for tool in ('cashflow', 'changes', 'data_quality')]
        reporter = EvidenceReporter()
        reporter._invoke = AsyncMock(side_effect=TimeoutError())
        reply, metadata = await reporter.answer('综合看看最近半年的财务情况，最值得注意的三个问题是什么？', {}, results)
        self.assertEqual(len(metadata.get('attentionPoints', [])), 3)
        self.assertIn('综合关注点', reply[:100])
        self.assertIn('1.', reply)
        self.assertIn('2.', reply)
        self.assertIn('3.', reply)
        self.assertIn('医疗', reply)
        self.assertIn('样本不足', reply)
        self.assertNotIn('抗风险能力较弱', reply)
