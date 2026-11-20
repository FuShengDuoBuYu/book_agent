import unittest
from unittest.mock import AsyncMock

from app.agent.artifacts import build_artifacts
from app.agent.evidence_report import EvidenceReporter
from app.schemas.report import ReportDraft
from tests.test_analysis_artifacts import forecast_result
from tests.test_conclusion_delivery import answer, statement


class ReportFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_cited_conclusion_call_preserves_full_private_evidence_and_scope(self):
        reporter = EvidenceReporter()
        reporter._invoke = AsyncMock(return_value=answer('{{j1}}', reasons=[
            statement('应把不同成员的来源分别核对，不要把历史样本当成未来到账。', 'f1')]))
        forecast = forecast_result()
        reply, meta = await reporter.answer('分别是谁赚的？', {'targetMonth': '2026-12'}, [forecast],
            history='旧回答虚构了774000元余额。', artifacts=build_artifacts([forecast]),
            understanding={'requestedAnswers': ['按成员和来源拆分']},
            analysis_scope={'mode': '家庭版', 'targetMonth': '2026-12'})
        self.assertEqual((meta['status'], meta['compositionStatus']), ('grounded', 'planned'))
        reporter._invoke.assert_awaited_once()
        schema, system, payload = reporter._invoke.await_args.args
        self.assertIs(schema, ReportDraft)
        self.assertEqual(payload['analysisScope']['mode'], '家庭版')
        self.assertEqual(payload['userConditions']['targetMonth'], '2026-12')
        self.assertNotIn('774000', repr(payload))
        self.assertIn('7,100', reply)
        self.assertIn('未来到账', reply)
        self.assertLess(len(reply), 700)
        self.assertGreater(len(meta['facts']), 1)
        self.assertTrue(meta['composerFactIds'])
        self.assertTrue(meta['valueReferences'])

    async def test_timeout_retains_known_answer_and_requested_source_evidence_without_full_dump(self):
        reporter = EvidenceReporter()
        reporter._invoke = AsyncMock(side_effect=TimeoutError())
        result = forecast_result()
        reply, meta = await reporter.answer('收入如何构成，怎么计算？', {}, [result], artifacts=build_artifacts([result]))
        self.assertEqual(meta['compositionStatus'], 'fallback')
        reporter._invoke.assert_awaited_once()
        self.assertIn('7,100', reply)
        self.assertIn('实习', reply)
        self.assertIn('带球', reply)
        self.assertNotIn('| ---', reply)
        self.assertLessEqual(len(reply), 1200)
        self.assertTrue(meta['attempts'])

    async def test_one_bounded_repair_keeps_reference_ids_unchanged(self):
        reporter = EvidenceReporter()
        seen = []
        async def invoke(schema, system, payload):
            seen.append(list(payload['values']))
            return answer('下月一定到账999999元。') if len(seen) == 1 else answer('{{j1}}')
        reporter._invoke = AsyncMock(side_effect=invoke)
        reply, meta = await reporter.answer('下个月收入多少？', {}, [forecast_result()])
        self.assertEqual(meta['compositionStatus'], 'planned')
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(reporter._invoke.await_count, 2)
        self.assertEqual(len(meta['attempts']), 1)
        self.assertNotIn('999999', reply)
        self.assertIn('7,100', reply)

    async def test_repeated_invalid_prose_stops_after_one_repair_and_records_both_calls(self):
        reporter = EvidenceReporter()
        reporter._invoke = AsyncMock(return_value=answer('收入肯定能翻倍。'))
        reply, meta = await reporter.answer('下个月收入多少？', {}, [forecast_result()])
        self.assertEqual(meta['compositionStatus'], 'fallback')
        self.assertEqual(reporter._invoke.await_count, 2)
        self.assertNotIn('肯定', reply)
        self.assertEqual(len(meta['calls']), 2)

    async def test_family_forecast_does_not_erase_personal_fixed_expense_audit(self):
        reporter = EvidenceReporter()
        reporter._invoke = AsyncMock(side_effect=TimeoutError())
        recurring = {'tool': 'recurring', 'mode': '个人版', 'metric': 'expense', 'evidence': {
            'expense': [{'source': '租房开支', 'activeMonths': 2, 'transactions': 2, 'typicalAmount': 100}]}}
        _, meta = await reporter.answer('个人固定开销和家庭收入预测', {}, [forecast_result(), recurring])
        self.assertTrue(any(f['binding']['scope'] == '个人版' and '租房开支' in f['text'] for f in meta['facts']))
        self.assertTrue(any(f['binding']['scope'] == '家庭版' for f in meta['facts']))
