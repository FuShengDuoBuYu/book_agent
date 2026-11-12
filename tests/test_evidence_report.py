import unittest
from unittest.mock import AsyncMock

from app.agent.evidence_report import EvidenceReporter, validate_draft, render_report, build_facts
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
