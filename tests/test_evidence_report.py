import unittest
from unittest.mock import AsyncMock

from app.agent.evidence_report import EvidenceReporter, validate_draft, render_report, build_facts, prepare_draft
from app.schemas.report import ReportDraft


class EvidenceReportTests(unittest.TestCase):
    def setUp(self):
        self.facts = [{'id': 'f1', 'text': '缺少可动用余额，不能判断购买是否影响生活。', 'required': True}]

    def draft(self, **kwargs):
        return ReportDraft.model_validate({'sections': [{'title': '购买影响', 'factIds': ['f1'],
                                                        'analysis': '先核对可用余额及近期必付账单。', **kwargs}]})

    def test_unknown_reference_rejected(self):
        self.assertTrue(validate_draft(self.draft(factIds=['fake']), self.facts))

    def test_model_arithmetic_rejected_including_chinese_money(self):
        for prose in ('你还可支撑1.5个月。', '可存三千元。'):
            self.assertTrue(validate_draft(self.draft(analysis=prose), self.facts))

    def test_renderer_preserves_fact_labels(self):
        result = render_report(self.draft(), self.facts)
        self.assertIn(self.facts[0]['text'], result)

    def test_missing_liquidity_is_a_mandatory_fact(self):
        facts = build_facts([{'tool': 'purchase', 'mode': '个人版', 'evidence': {
            'sufficientData': False, 'missingInputs': ['startingBalance'], 'purchaseAmount': 6000}}])
        self.assertTrue(any(f['required'] and '可动用余额' in f['text'] for f in facts))

    def test_expense_category_names_are_preserved(self):
        facts = build_facts([{'tool': 'cashflow', 'evidence': {'computedConclusion': '月均',
            'expenseCategories': [{'name': '房租', 'amount': 1800, 'shareOfIncomePercent': 27.27}],
            'interpretation': '资产未知'}}])
        self.assertTrue(any('房租' in fact['text'] for fact in facts))

    def test_savings_time_to_goal_is_available_to_the_answer(self):
        from app.tools.financial_analysis import run_analysis
        from tests.test_decision_analysis import ledger
        from datetime import date
        for starting, expected in ((10000, '3 个月'), (20000, '已经达到')):
            evidence = run_analysis('savings', ledger(), as_of=date(2026, 11, 12), starting_balance=starting, target_amount=20000)
            facts = build_facts([{'tool': 'savings', 'evidence': evidence}])
            self.assertTrue(any(expected in fact['text'] and fact['required'] for fact in facts))

    def test_unconfirmed_categorical_claims_are_not_sent(self):
        facts = [{'id': 'f1', 'required': True, 'text': '检测到疑似重复组 0 组；未来医疗安排未知。'}]
        draft = self.draft(title='医疗支出突增且记录不完整',
            analysis='账本中存在疑似重复记录。家教属于不稳定的兼职收入。近期增加是一次性的医疗支出。建议确认后续是否有就诊计划。')
        prepared, _ = prepare_draft(draft, facts)
        reply = render_report(prepared, facts)
        self.assertNotIn('存在疑似重复', reply)
        self.assertNotIn('不稳定的兼职', reply)
        self.assertNotIn('一次性的医疗', reply)
        self.assertNotIn('记录不完整', reply)
        self.assertIn('建议确认后续是否有就诊计划', reply)

    def test_numeric_only_lead_does_not_discard_useful_later_analysis(self):
        draft = ReportDraft.model_validate({'sections': [
            {'title': '结余测算', 'factIds': ['f1'], 'analysis': '预计新增12000元。'},
            {'title': '需确认的变化', 'factIds': ['f1'], 'analysis': '建议确认后续收入安排，再据实际到账情况调整计划。'}]})
        prepared, _ = prepare_draft(draft, self.facts)
        self.assertEqual(prepared.sections[0].analysis, '')
        self.assertIn('建议确认', render_report(prepared, self.facts))

    def test_advice_does_not_turn_unknown_recurrence_into_a_fact(self):
        facts = [{'id': 'f1', 'required': True, 'text': '医疗记录备注：一次就诊。不能确认是否再次发生。'}]
        draft = self.draft(analysis='建议将这笔一次性支出从常规预算中剔除。若确认后续无需复诊，再单独评估预算。')
        prepared, _ = prepare_draft(draft, facts)
        self.assertNotIn('一次性支出', prepared.sections[0].analysis)
        self.assertIn('若确认', prepared.sections[0].analysis)

    def test_no_identified_candidates_does_not_prove_no_adjustment_possible(self):
        facts = [{'id': 'f1', 'required': True, 'text': '可逐项核查的月度支出上界为 0 元；不能证明现实没有优化空间。'}]
        draft = self.draft(analysis='其他所有支出均无调整余地。常规支出已无进一步压缩的可能。建议核对未分类项目，并考虑增收或延长期限。')
        prepared, _ = prepare_draft(draft, facts)
        self.assertNotIn('无调整余地', prepared.sections[0].analysis)
        self.assertNotIn('无进一步压缩', prepared.sections[0].analysis)
        self.assertIn('考虑增收或延长期限', prepared.sections[0].analysis)

    def test_protected_expenses_leave_a_concrete_next_step_when_no_cut_is_identified(self):
        from datetime import date
        from app.tools.financial_analysis import run_analysis
        from tests.test_decision_analysis import ledger
        evidence = run_analysis('optimization', ledger(), as_of=date(2026, 11, 13),
                                protected_categories=['房租', '餐饮'])
        facts = build_facts([{'tool': 'optimization', 'evidence': evidence}])
        self.assertTrue(any(fact['required'] and '增收' in fact['text'] and '保护项' in fact['text'] for fact in facts))

    def test_chinese_proportions_cannot_change_the_scope_of_a_scenario(self):
        draft = self.draft(analysis='在收入减少一半的情况下，压力会增加。建议确认家教后续安排。')
        prepared, _ = prepare_draft(draft, self.facts)
        self.assertNotIn('收入减少一半', prepared.sections[0].analysis)
        self.assertIn('确认家教', prepared.sections[0].analysis)

    def test_source_labels_and_available_cash_do_not_guarantee_future_safety(self):
        draft = self.draft(analysis='固定工资通常具有较好的稳定性。若当前余额允许，这笔支出不会导致生活困难。请核对近期必付账单和应急储备。')
        prepared, _ = prepare_draft(draft, self.facts)
        self.assertNotIn('较好的稳定性', prepared.sections[0].analysis)
        self.assertNotIn('不会导致生活困难', prepared.sections[0].analysis)
        self.assertIn('核对近期必付账单', prepared.sections[0].analysis)

    def test_positive_reserve_margin_cannot_be_reported_as_a_funding_gap(self):
        facts = build_facts([{'tool': 'purchase', 'evidence': {'sufficientData': True,
            'startingBalance': 12000, 'purchaseAmount': 6000, 'cashAfterPurchase': 6000,
            'reserveAmount': 5000, 'reserveMargin': 1000}}])
        self.assertTrue(any(f['required'] and '不存在这两项的资金缺口' in f['text'] for f in facts))
        draft = self.draft(title='资金缺口分析', analysis='需要收入及时到账以填补该缺口。请核对近期必付账单。')
        prepared, _ = prepare_draft(draft, facts)
        self.assertNotIn('填补该缺口', prepared.sections[0].analysis)
        self.assertNotIn('资金缺口', prepared.sections[0].title)
        self.assertIn('核对近期必付账单', prepared.sections[0].analysis)


class EvidenceReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_numeric_restatement_removed_but_qualitative_advice_reviewed(self):
        reporter = EvidenceReporter.__new__(EvidenceReporter)
        draft = ReportDraft.model_validate({'sections': [{'title': '购买影响', 'factIds': ['f1'],
            'analysis': '你可以支撑1.5个月。先核对近期必付账单，再决定是否购买。'}]})
        reporter._draft = AsyncMock(return_value=draft)
        reporter._review = AsyncMock(return_value=[])
        reply, result = await reporter.answer('能买么', {}, [{'tool': 'purchase', 'evidence': {
            'sufficientData': False, 'missingInputs': ['startingBalance']}}])
        self.assertNotIn('1.5', reply)
        self.assertIn('先核对近期必付账单', reply)
        self.assertEqual(result['status'], 'reviewed')

    async def test_rejected_interpretation_is_never_returned(self):
        reporter = EvidenceReporter.__new__(EvidenceReporter)
        bad = ReportDraft.model_validate({'sections': [{'title': '购买影响', 'factIds': ['f1'],
                                                       'analysis': '你肯定买不起，会严重影响生活。'}]})
        reporter._draft = AsyncMock(return_value=bad)
        reporter._review = AsyncMock(return_value=['没有余额不能判断购买能力'])
        reply, validation = await reporter.answer('能否购买', {}, [{'tool': 'purchase', 'mode': '个人版',
                        'evidence': {'sufficientData': False, 'missingInputs': ['startingBalance']}}])
        self.assertNotIn('肯定买不起', reply)
        self.assertEqual(validation['status'], 'verified_facts_fallback')
        self.assertIn('可动用余额', reply)
