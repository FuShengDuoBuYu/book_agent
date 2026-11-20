import unittest
from copy import deepcopy
from unittest.mock import AsyncMock

from app.agent.report_composition import (REPORT_PROMPT, REPORT_TOKENS, answer_schema,
    compose_report, grounded_analysis, render_report, validate_draft)
from app.agent.report_evidence import prepare_report_context
from app.schemas.report import ReportDraft


def fact(key, text='已核算的账单。', *, group='g1', tool='changes', scope='个人版', values=None, **binding):
    return {'id': key, 'text': text, 'required': True, 'values': values or [],
            'binding': {'resultGroup': group, 'tool': tool, 'scope': scope, 'primary': True, **binding}}


def context(facts=None, findings=None, question='分析变化，给我结论', conditions=None, **kwargs):
    facts = facts or [fact('f1', values=[{'label': '支出变化', 'value': 900, 'unit': '元'}])]
    findings = findings or {'g1': {'scope': '个人版', 'tool': 'changes',
                                 'observations': ['已记录支出上升。'], 'states': {}}}
    return prepare_report_context({'question': question, 'facts': facts, 'analysisFindings': findings,
        'understanding': {'requestedAnswers': ['解释变化']}, 'userConditions': conditions or {}},
        REPORT_PROMPT, ReportDraft, **kwargs)


def draft(text='支出变化集中在已记录的大额项目，应先核对其用途再判断是否持续。', ids=('f1',), **kwargs):
    return ReportDraft(conclusion={'text': text, 'factIds': list(ids)}, **kwargs)


class ConclusionCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_can_synthesize_a_conclusion_instead_of_selecting_canned_prose(self):
        data = context()
        invoke = AsyncMock(return_value=draft().model_dump())
        result = await compose_report(data, invoke)
        self.assertEqual(validate_draft(result, data), [])
        self.assertIn('应先核对其用途', render_report(result, data))
        self.assertNotIn('已记录支出上升', render_report(result, data))
        invoke.assert_awaited_once()
        self.assertIs(invoke.await_args.args[0], ReportDraft)

    async def test_only_current_evidence_and_conditions_reach_the_composer(self):
        data = context()
        original = deepcopy(data.payload)
        invoke = AsyncMock(return_value=draft().model_dump())
        await compose_report(data, invoke)
        self.assertEqual(data.payload, original)
        self.assertNotIn('history', invoke.await_args.args[2])
        self.assertNotIn('artifacts', invoke.await_args.args[2])
        self.assertIn('computedFindings', invoke.await_args.args[2])

    def test_schema_references_only_facts_present_in_the_bounded_context(self):
        data = context()
        schema = answer_schema(data.payload)
        self.assertEqual(schema['$defs']['ReportStatement']['properties']['factIds']['items']['enum'], ['f1'])
        self.assertNotIn('sections', schema['properties'])

    def test_typed_number_is_substituted_only_with_its_exact_fact(self):
        data = context()
        report = draft('这项已记支出变化为{{v1}}，应核对是否对应后续安排。')
        self.assertEqual(validate_draft(report, data), [])
        self.assertIn('900 元', render_report(report, data))
        self.assertNotIn('{{v1}}', render_report(report, data))

    def test_arabic_and_chinese_fabricated_amounts_are_rejected(self):
        for text in ['预计能赚999999元。', '预计能赚两万元。', '明年收入翻三倍并保证到账。']:
            with self.subTest(text=text):
                self.assertTrue(validate_draft(draft(text), context()))

    def test_unknown_reference_and_foreign_value_ownership_are_rejected(self):
        facts = [fact('f1'), fact('f2', group='g2', scope='家庭版',
                                values=[{'label': '金额', 'value': 900, 'unit': '元'}])]
        data = context(facts=facts)
        self.assertTrue(validate_draft(draft('收入为{{v9999}}。'), data))
        self.assertTrue(validate_draft(draft('收入为{{v1}}。'), data))
        self.assertTrue(validate_draft(draft(ids=('unknown',)), data))

    def test_same_amount_does_not_make_a_historical_sample_a_forecast(self):
        data = context(facts=[fact('f1', values=[{'label': '该来源历史样本入账', 'value': 900, 'unit': '元'}])])
        self.assertTrue(validate_draft(draft('预计下个月收入为{{v1}}。'), data))

    def test_expense_share_cannot_be_relabelled_as_share_of_income(self):
        data = context(facts=[fact('f1', tool='aggregate', metric='expense',
            values=[{'label': '同区间筛选后方向占比', 'value': 20, 'unit': '%'}])],
            findings={'g1': {'tool': 'aggregate', 'scope': '个人版', 'states': {}}})
        self.assertTrue(validate_draft(draft('这个项目占收入{{v1}}。'), data))

    def test_cross_book_statement_must_identify_both_books(self):
        data = context(facts=[fact('f1'), fact('f2', group='g2', scope='家庭版')],
            findings={'g1': {'tool': 'changes', 'scope': '个人版', 'states': {}},
                      'g2': {'tool': 'changes', 'scope': '家庭版', 'states': {}}})
        self.assertTrue(validate_draft(draft('收入来源比较集中。', ('f1', 'f2')), data))
        self.assertEqual(validate_draft(draft('个人版和家庭版分别核算，不能混作个人余额。', ('f1', 'f2')), data), [])

    def test_protected_spend_cannot_become_a_cut_recommendation(self):
        data = context(conditions={'protectedCategories': ['房租']})
        self.assertTrue(validate_draft(draft('建议先削减房租。'), data))
        self.assertEqual(validate_draft(draft('保留房租，先核查其他项目的用途。'), data), [])

    def test_certain_negative_cause_is_not_justified_by_an_evidence_reference(self):
        self.assertTrue(validate_draft(draft('这并非学费造成的现金流变化。'), context()))
        self.assertEqual(validate_draft(draft('不能据此断定现实消费原因，还需核对转账用途。'), context()), [])

    def test_misbound_findings_do_not_authorize_a_purchase_decision(self):
        with self.assertRaisesRegex(ValueError, '范围'):
            context(facts=[fact('f1', tool='purchase')],
                    findings={'g1': {'tool': 'purchase', 'scope': '家庭版', 'states': {'coversPrice': True}}})

    def test_critical_decision_lead_cannot_be_freely_reinterpreted(self):
        data = context(facts=[fact('f1', tool='purchase')], findings={
            'g1': {'tool': 'purchase', 'scope': '个人版', 'states': {
                'sufficientData': True, 'coversPrice': True, 'coversPriceAndReserve': False}}})
        self.assertTrue(validate_draft(draft('我看这样就稳了。'), data))
        fixed = draft('{{j1}}')
        self.assertEqual(validate_draft(fixed, data), [])
        self.assertIn('达不到', render_report(fixed, data))

    def test_missing_reserve_is_unknown_and_does_not_reverse_known_payment_capacity(self):
        data = context(facts=[fact('f1', tool='purchase')], findings={
            'g1': {'tool': 'purchase', 'scope': '个人版', 'states': {
                'sufficientData': True, 'coversPrice': True, 'coversPriceAndReserve': None}}})
        self.assertIn('可以支付', data.judgments[0]['text'])
        self.assertIn('尚未给出', data.judgments[0]['text'])
        self.assertNotIn('买不起', data.judgments[0]['text'])

    def test_long_notes_are_bounded_without_losing_full_audit(self):
        facts = [fact(f'f{i}', text='原始长备注。' * 1000) for i in range(1, 70)]
        data = context(facts=facts)
        self.assertEqual(len(data.facts), 69)
        self.assertLessEqual(len(data.selected), 36)
        self.assertLess(data.budget['estimatedPromptTokens'], data.budget['promptBudgetTokens'])
        self.assertEqual(data.facts[0]['text'], facts[0]['text'])

    def test_reference_ids_are_stable_when_the_context_budget_changes(self):
        facts = [fact(f'f{i}', text='账单内容。' * 30,
                      values=[{'label': '金额', 'value': i, 'unit': '元'}]) for i in range(1, 50)]
        wide, narrow = context(facts=facts), context(facts=facts, num_ctx=5000)
        index = lambda data: {(v['factId'], v['role']): v['id'] for v in data.values}
        self.assertEqual(index(wide), index(narrow))
        self.assertLess(len(narrow.selected), len(wide.selected))

    def test_numeric_ledger_label_is_inert_and_not_recursively_substituted(self):
        data = context(facts=[fact('f1', source='画稿{{v1}}',
                                  values=[{'label': '已记收入', 'value': 900, 'unit': '元'}])])
        label = next(v['id'] for v in data.values if v['unit'] == 'label')
        report = draft('需要核对{{' + label + '}}的原始用途，金额为{{v1}}。')
        self.assertEqual(validate_draft(report, data), [])
        self.assertIn('画稿{{v1}}', render_report(report, data))
        self.assertIn('900 元', render_report(report, data))

    def test_length_applies_to_complete_reply_and_explicit_detail_can_expand(self):
        report = draft('核对原始账单并说明边界。' * 110)
        self.assertTrue(validate_draft(report, context()))
        self.assertEqual(validate_draft(report, context(question='详细展开分析')), [])


class GroundedAnalysisTests(unittest.TestCase):
    def test_private_computed_findings_remain_scope_checked_and_deduplicated(self):
        facts = [fact('f1')]
        seen = set()
        findings = {'g1': {'tool': 'changes', 'scope': '个人版', 'observations': ['本范围已核对。']}}
        self.assertEqual(grounded_analysis(['f1'], facts, findings, seen), '【个人版】本范围已核对。')
        self.assertEqual(grounded_analysis(['f1'], facts, findings, seen), '')
        with self.assertRaisesRegex(ValueError, '范围'):
            grounded_analysis(['f1'], facts, {'g1': {**findings['g1'], 'scope': '家庭版'}})
