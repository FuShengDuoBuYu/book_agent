import unittest
from unittest.mock import AsyncMock, patch

from app.agent.artifacts import build_artifacts
from app.agent.evidence_report import EvidenceReporter
from app.agent.report_composition import ReportOutline
from app.schemas.report import ReportDraft
from tests.test_analysis_artifacts import forecast_result


def missing_balance():
    return {'tool': 'purchase', 'mode': '家庭版', 'evidence': {
        'sufficientData': False, 'missingInputs': ['startingBalance'], 'purchaseAmount': 700,
    }}


class ReportFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_reporter_calls_only_outline_and_renders_approved_scoped_findings(self):
        reporter = EvidenceReporter()
        reporter._invoke = AsyncMock(return_value=ReportOutline(sections=[
            {'title': '收入构成', 'answerGoal': '按成员和来源拆分', 'factIds': ['f1']},
        ]))
        forecast = forecast_result()
        artifacts = build_artifacts([forecast])
        scope = {'mode': '家庭版', 'targetMonth': '2026-12'}
        reply, validation = await reporter.answer(
            '分别是谁赚的？', {'targetMonth': '2026-12'}, [forecast],
            history='助手: 上一次提到不应沿用的余额7,740元。',
            instruction='按成员和来源解释上轮预测。',
            artifacts=artifacts, understanding={'requestedAnswers': ['按成员和来源拆分']},
            analysis_scope=scope,
        )
        self.assertEqual((validation['status'], validation['compositionStatus']), ('grounded', 'planned'))
        reporter._invoke.assert_awaited_once()
        schema, _, payload = reporter._invoke.await_args.args
        self.assertIs(schema, ReportOutline)
        self.assertEqual(payload['answerGoals'], ['按成员和来源拆分'])
        self.assertEqual(payload['analysisScope'], scope)
        self.assertEqual(payload['userConditions']['targetMonth'], '2026-12')
        self.assertNotIn('7,740', repr(payload))
        self.assertNotIn('analysisFindings', payload)
        self.assertIn('成员乙', reply)
        self.assertIn('7,100', reply)
        observations = validation['findings']['g1']['observations']
        self.assertTrue(observations)
        for statement in observations:
            self.assertIn(statement, reply)

    async def test_outline_timeout_uses_code_explanations_required_facts_and_table(self):
        reporter = EvidenceReporter()
        reporter._invoke = AsyncMock(side_effect=TimeoutError())
        forecast = forecast_result()
        reply, validation = await reporter.answer(
            '收入如何构成，怎么计算？', {}, [forecast], artifacts=build_artifacts([forecast]),
        )
        self.assertEqual((validation['status'], validation['compositionStatus']), ('grounded', 'fallback'))
        reporter._invoke.assert_awaited_once()
        self.assertIs(reporter._invoke.await_args.args[0], ReportOutline)
        for name in ('带球', '实习', '7,100'):
            self.assertIn(name, reply)
        self.assertNotIn('支出已恢复', reply)
        finding = validation['findings']['g1']
        self.assertTrue(finding['observations'])
        for text in finding['observations'] + finding['limitations']:
            self.assertIn(text, reply)
        required = [row['text'] for row in validation['facts'] if row['required']]
        self.assertTrue(required)
        self.assertTrue(all(text in reply for text in required))

    async def test_model_cannot_smuggle_free_analysis_into_an_outline(self):
        reporter = EvidenceReporter()
        reporter._invoke = AsyncMock(return_value={'sections': [{
            'title': '需要确认的事项', 'answerGoal': '能否购买？', 'factIds': ['f1'],
            'analysis': '你肯定买不起，会严重影响生活。',
        }]})
        reply, validation = await reporter.answer('能否购买？', {}, [missing_balance()])
        self.assertEqual((validation['status'], validation['compositionStatus']), ('grounded', 'fallback'))
        reporter._invoke.assert_awaited_once()
        self.assertNotIn('肯定买不起', reply)
        self.assertIn('可动用余额', reply)

    async def test_reporter_rejects_a_draft_with_free_analysis_even_with_known_references(self):
        reporter = EvidenceReporter()
        reporter._invoke = AsyncMock()
        bad = ReportDraft(sections=[{'title': '需要确认的事项', 'factIds': ['f1', 'f2'],
                                    'analysis': '你肯定买不起。'}])
        with patch('app.agent.evidence_report.compose_report', AsyncMock(return_value=bad)):
            reply, validation = await reporter.answer('能否购买？', {}, [missing_balance()])
        self.assertEqual((validation['status'], validation['compositionStatus']), ('grounded', 'fallback'))
        reporter._invoke.assert_not_awaited()
        self.assertNotIn('肯定买不起', reply)
        self.assertIn('可动用余额', reply)

    async def test_unapproved_title_cannot_introduce_a_purchase_verdict(self):
        from app.agent.report_composition import compose_report

        reporter = EvidenceReporter()
        reporter._invoke = AsyncMock(return_value=ReportOutline(sections=[
            {'title': '需要确认的事项', 'answerGoal': '能否购买？', 'factIds': ['f1']},
        ]))

        async def spoof_title(*args, **kwargs):
            valid = await compose_report(*args, **kwargs)
            valid.sections[0].title = '一定买不起'
            return valid

        with patch('app.agent.evidence_report.compose_report', side_effect=spoof_title):
            reply, validation = await reporter.answer('能否购买？', {}, [missing_balance()])
        self.assertEqual((validation['status'], validation['compositionStatus']), ('grounded', 'fallback'))
        reporter._invoke.assert_awaited_once()
        self.assertNotIn('一定买不起', reply)
        self.assertIn('可动用余额', reply)

    async def test_fallback_preserves_required_facts_beyond_the_old_display_limit(self):
        reporter = EvidenceReporter()
        reporter._invoke = AsyncMock(side_effect=TimeoutError())
        facts = [{'id': f'f{i}', 'text': f'必要条件甲{"乙" * i}。', 'required': True,
                  'binding': {'resultGroup': 'g1', 'scope': '家庭版', 'tool': 'purchase'}}
                 for i in range(1, 14)]
        with patch('app.agent.evidence_report.build_facts', return_value=facts):
            reply, validation = await reporter.answer('逐项核查必要条件', {}, [missing_balance()])
        self.assertEqual((validation['status'], validation['compositionStatus']), ('grounded', 'fallback'))
        self.assertTrue(all(row['text'] in reply for row in facts))
        self.assertIn('可动用余额', reply)

    async def test_family_forecast_does_not_remove_requested_personal_recurring_expenses(self):
        reporter = EvidenceReporter()
        reporter._invoke = AsyncMock(side_effect=TimeoutError())
        forecast = forecast_result()
        recurring = {'tool': 'recurring', 'mode': '个人版', 'metric': 'expense', 'evidence': {
            'expense': [{'source': '租房开支', 'activeMonths': 2, 'transactions': 2, 'typicalAmount': 100}],
        }}
        reply, validation = await reporter.answer('个人固定开销和家庭收入预测', {}, [forecast, recurring])
        self.assertIn('租房开支', reply)
        self.assertTrue(any(row['binding']['scope'] == '个人版' and '租房开支' in row['text']
                            for row in validation['facts']))
        self.assertIn('【家庭版】', reply)
        self.assertIn('【个人版】', reply)


if __name__ == '__main__':
    unittest.main()
