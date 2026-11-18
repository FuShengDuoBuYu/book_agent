import unittest
from unittest.mock import AsyncMock

from app.agent.evidence_report import EvidenceReporter, prepare_draft, render_report, validate_draft
from app.schemas.report import ReportDraft
from tests.test_analysis_artifacts import forecast_result


class ReportContractTests(unittest.TestCase):
    def test_ledger_links_in_analysis_are_literal_while_emphasis_is_retained(self):
        facts = [{'id': 'f1', 'text': '记录仅提供备注，不能证明现实原因。', 'required': True}]
        draft = ReportDraft.model_validate({'sections': [{'title': '备注核查', 'factIds': ['f1'],
            'analysis': '**建议核查**：备注中的 [结算地址](https://ledger.example.test/pay) 和 https://ledger.example.test/info 仅作为记录内容。'}]})
        reply = render_report(draft, facts)
        self.assertIn('**建议核查**', reply)
        self.assertIn('` [结算地址](https://ledger.example.test/pay) `', reply)
        self.assertIn('` https://ledger.example.test/info `', reply)

    def test_remark_numbers_cannot_be_promoted_to_income_values(self):
        facts = [{'id': 'f1', 'text': '收入 100 元，票据编号 777。', 'required': True,
                  'values': [{'label': '收入', 'value': 100, 'unit': '元'}]}]
        from app.agent.evidence_report import value_catalog
        self.assertFalse(any('777' in row['display'] for row in value_catalog(facts)))

    def test_value_reference_requires_its_own_fact_and_its_own_unit(self):
        facts = [{'id': 'f1', 'text': '收入 100 元。', 'required': False,
                  'values': [{'label': '收入', 'value': 100, 'unit': '元'}]},
                 {'id': 'f2', 'text': '支出 50 元。', 'required': False,
                  'values': [{'label': '支出', 'value': 50, 'unit': '元'}]}]
        for prose in ('收入为{{v2}}。', '收入为{{v1}}天。'):
            draft = ReportDraft.model_validate({'sections': [{'title': '核算', 'factIds': ['f1'], 'analysis': prose}]})
            self.assertTrue(validate_draft(draft, facts))

    def test_completion_verdict_is_required_from_the_reviewer(self):
        from pydantic import ValidationError
        from app.schemas.report import ReportReview
        with self.assertRaises(ValidationError):
            ReportReview.model_validate({'supported': True, 'issues': []})

    def test_evidence_reference_can_be_used_inside_a_numeric_explanation(self):
        facts = [{'id': 'f1', 'text': '在相同条件下，预计收入 5,000 元。', 'required': True,
                  'values': [{'label': '预计收入', 'value': 5000, 'unit': '元'}]}]
        draft = ReportDraft.model_validate({'sections': [{'title': '来源与条件', 'factIds': ['f1'],
            'analysis': '这个来源的基准是{{v1}}，后续工作安排变化时需要重算。'}]})
        self.assertEqual(validate_draft(draft, facts), [])
        self.assertIn('基准是5,000 元', render_report(draft, facts))

    def test_verified_amount_and_calendar_can_be_quoted_naturally(self):
        facts = [{'id': 'f1', 'text': '2027-01 条件预测收入 100 元。', 'required': True,
                  'values': [{'label': '目标月份', 'value': '2027-01', 'unit': 'month'},
                             {'label': '预测收入', 'value': 100, 'unit': '元'}]}]
        draft = ReportDraft.model_validate({'sections': [{'title': '2027年1月收入预测', 'factIds': ['f1'],
            'analysis': '2027-01 的条件估计是100元，后续项目安排变化时应重新计算。'}]})
        self.assertEqual(validate_draft(draft, facts), [])

    def test_natural_month_range_uses_the_typed_calendar_endpoints(self):
        facts = [{'id': 'f1', 'text': '参考月份 2026-08、2026-09、2026-10。', 'required': True,
            'values': [{'label': '参考月份', 'value': month, 'unit': 'month'}
                       for month in ('2026-08', '2026-09', '2026-10')]}]
        draft = ReportDraft.model_validate({'sections': [{'title': '参考区间', 'factIds': ['f1'],
            'analysis': '参考2026年8月至10月的已记账数据，需要确认后续工作安排。'}]})
        self.assertEqual(validate_draft(draft, facts), [])

    def test_known_money_range_can_share_its_unit(self):
        facts = [{'id': 'f1', 'text': '历史情景 3000～3700 元。', 'required': True,
            'values': [{'label': '下界', 'value': 3000, 'unit': '元'}, {'label': '上界', 'value': 3700, 'unit': '元'}]}]
        draft = ReportDraft.model_validate({'sections': [{'title': '条件范围', 'factIds': ['f1'],
            'analysis': '历史情景范围是3,000～3,700元，这不是置信区间。'}]})
        self.assertEqual(validate_draft(draft, facts), [])

    def test_unsupported_number_triggers_retry_instead_of_silently_deleting_text(self):
        facts = [{'id': 'f1', 'text': '可动用余额未知。', 'required': True}]
        draft = ReportDraft.model_validate({'sections': [{'title': '购买影响', 'factIds': ['f1'],
            'analysis': '你可以再撑9个月。请减少开销。'}]})
        with self.assertRaisesRegex(ValueError, '数量|依据'):
            prepare_draft(draft, facts)


class ReportFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_answer_amounts_do_not_prime_fresh_fact_generation(self):
        reporter = EvidenceReporter()
        reporter._draft = AsyncMock(return_value=ReportDraft.model_validate({'sections': [
            {'title': '来源解释', 'factIds': ['f1'], 'analysis': '应以本轮重新核算的来源和条件解释结果。'}]}))
        reporter._review = AsyncMock(return_value=[])
        await reporter.answer('按刚才情况解释', {'targetMonth': '2027-01', 'activeAnalyses': [{'tool': 'forecast'}]},
            [forecast_result()], history='助手: 2027-01 净额变化7,740元，但不是当前余额。')
        payload = reporter._draft.await_args.args[0]
        self.assertNotIn('7,740', payload['history'])
        self.assertIn('2027-01', payload['history'])
        self.assertNotIn('activeAnalyses', payload['userConditions'])
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
