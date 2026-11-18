import unittest
from unittest.mock import AsyncMock

from app.agent.evidence_report import EvidenceReporter, prepare_draft, render_report, validate_draft
from app.schemas.report import ReportDraft
from tests.test_analysis_artifacts import forecast_result


class ReportContractTests(unittest.TestCase):
    def test_evidence_reference_can_be_used_inside_a_numeric_explanation(self):
        facts = [{'id': 'f1', 'text': '在相同条件下，预计收入 5,000 元。', 'required': True}]
        draft = ReportDraft.model_validate({'sections': [{'title': '来源与条件', 'factIds': ['f1'],
            'analysis': '这个来源的基准是{{v1}}元，后续工作安排变化时需要重算。'}]})
        self.assertEqual(validate_draft(draft, facts), [])
        self.assertIn('基准是5,000元', render_report(draft, facts))

    def test_unsupported_number_triggers_retry_instead_of_silently_deleting_text(self):
        facts = [{'id': 'f1', 'text': '可动用余额未知。', 'required': True}]
        draft = ReportDraft.model_validate({'sections': [{'title': '购买影响', 'factIds': ['f1'],
            'analysis': '你可以再撑9个月。请减少开销。'}]})
        with self.assertRaisesRegex(ValueError, '数量|依据'):
            prepare_draft(draft, facts)


class ReportFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_goal_and_prior_answer_reach_both_writer_and_reviewer(self):
        from app.agent.artifacts import build_artifacts
        reporter = EvidenceReporter()
        reporter._draft = AsyncMock(return_value=ReportDraft.model_validate({'sections': [
            {'title': '收入构成', 'factIds': ['f1'], 'analysis': '表中按成员和来源拆分，后续安排改变时应分别调整。'}]}))
        reporter._review = AsyncMock(return_value=[])
        artifacts = build_artifacts([forecast_result()])
        reply, validation = await reporter.answer('分别是谁赚的？', {}, [forecast_result()],
            history='助手: 这是十二月家庭收入预测。', instruction='按成员和来源解释上轮预测，不要重复总额。',
            artifacts=artifacts, understanding={'requestedAnswers': ['按成员和来源拆分']})
        self.assertIn('成员乙', reply)
        self.assertEqual(validation['status'], 'reviewed')
        for gateway in (reporter._draft, reporter._review):
            payload = gateway.await_args.args[0]
            self.assertIn('十二月', payload['history'])
            self.assertIn('成员', payload['instruction'])
            self.assertTrue(payload['artifacts'])

    async def test_model_failure_keeps_relevant_source_table_and_explanation(self):
        from app.agent.artifacts import build_artifacts
        reporter = EvidenceReporter()
        reporter._draft = AsyncMock(side_effect=TimeoutError())
        reply, validation = await reporter.answer('收入如何构成，怎么计算？', {}, [forecast_result()],
            artifacts=build_artifacts([forecast_result()]))
        self.assertIn('带球', reply)
        self.assertIn('实习', reply)
        self.assertIn('7,100', reply)
        self.assertNotIn('支出已恢复', reply)
        self.assertEqual(validation['status'], 'computed_fallback')


if __name__ == '__main__':
    unittest.main()
