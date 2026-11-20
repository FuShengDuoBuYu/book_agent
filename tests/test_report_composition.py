import unittest
from copy import deepcopy
from unittest.mock import AsyncMock

from app.agent.report_composition import (REPORT_PROMPT, REPORT_TOKENS, answer_schema,
    compose_report, grounded_analysis, render_report, validate_draft, fallback_report)
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

    def test_forecast_basis_question_leads_with_explanation_instead_of_repeating_point(self):
        data = context(facts=[fact('f1', '下月收入条件估计为 6600 元。', tool='forecast', headline=True)],
            findings={'g1': {'tool': 'forecast', 'scope': '个人版', 'states': {'sufficientData': True}}},
            question='你为什么这样预测，依据是什么？')
        report = draft('这是按近期同来源的历史入账构成估计，不代表未来到账保证。')
        self.assertFalse(data.payload['answerRequirements']['leadWithJudgment'])
        self.assertEqual(validate_draft(report, data), [])
        self.assertTrue(render_report(report, data).startswith('这是按近期'))
        self.assertNotIn('6600', render_report(report, data))

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
        for text in ['预计能赚999999元。', '预计能赚两万元。', '明年收入翻三倍并保证到账。', '预计收入提高一半。', '这笔款半个月内到账。']:
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

    def test_internal_planning_bulk_cannot_crowd_out_months_and_change_sources(self):
        facts = [fact('warning', text='通用统计口径说明。' * 20, tool='trends')]
        facts += [fact(f'm{i}', text=f'月份账单已经核算，主要金额如下。', tool='trends',
                       month=f'2026-{i:02}', aggregation='recorded_month_total',
                       values=[{'label': '月份', 'value': f'2026-{i:02}', 'unit': 'month'},
                               {'label': '已记支出', 'value': i * 900, 'unit': '元'}]) for i in range(6, 12)]
        facts += [fact('driver', text='学费是已记大额支出，需要核查是否重复。', group='g2', tool='aggregate',
                       values=[{'label': 'expense', 'value': 12000, 'unit': '元'}])]
        payload = {'question': '分析近半年收支变化和主要原因', 'facts': facts,
                   'analysisFindings': {'g1': {'scope': '个人版', 'tool': 'trends', 'states': {}},
                                        'g2': {'scope': '个人版', 'tool': 'aggregate', 'states': {}}},
                   'analysisScope': {'historyWindow': None,
                       'primaryTasks': [{'tool': 'aggregate', 'queryIds': ['opaque-query-id' * 8],
                                         'unusedOptional': None} for _ in range(12)],
                       'sourceGroups': [{'label': '业务来源', 'sourceKeys': ['opaque-source-key' * 8] * 40}]},
                   'userConditions': {'sourceGroups': [{'label': '已确认来源', 'sourceKeys': ['opaque-key' * 8] * 40}]}}
        data = prepare_report_context(payload, REPORT_PROMPT, ReportDraft)
        self.assertTrue({f'm{i}' for i in range(6, 12)} | {'driver'} <= {f['id'] for f in data.selected})
        self.assertNotIn('opaque-key', repr(data.payload))
        self.assertNotIn('opaque-source-key', repr(data.payload))

    def test_causal_detail_is_not_dropped_behind_repeated_aggregate_totals(self):
        facts = [fact(f't{i}', text='按查询范围汇总得到总计。', tool='aggregate',
                      values=[{'label': 'expense', 'value': 100, 'unit': '元'}]) for i in range(1, 44)]
        facts.append(fact('cause', text='month=九月、source=学费：支出12000元。', tool='aggregate',
                          values=[{'label': 'expense', 'value': 12000, 'unit': '元'}]))
        data = context(facts=facts, findings={'g1': {'tool': 'aggregate', 'scope': '个人版', 'states': {}}})
        self.assertIn('cause', {f['id'] for f in data.selected})

    def test_typed_change_driver_survives_many_optional_totals(self):
        facts = [fact(f't{i}', tool='changes', values=[{'label': '支出', 'value': 100, 'unit': '元'}]) for i in range(44)]
        facts.append(fact('cause', tool='changes', evidenceRole='change_driver', category='医疗', metric='expense',
            text='医疗是该期支出变化的账面贡献。', values=[{'label': '金额变化', 'value': 900, 'unit': '元'}]))
        data = context(facts=facts)
        self.assertIn('cause', {f['id'] for f in data.selected})

    def test_monthly_improvement_gap_is_not_solved_by_postponing_a_goal(self):
        data = context(facts=[fact('f1', tool='optimization')], findings={
            'g1': {'tool': 'optimization', 'scope': '个人版', 'states': {},
                   'observations': ['候选开销不足以覆盖每月改善目标。']}})
        report = draft('{{j1}}', nextStep={'text': '如果目标期限可以调整，也可考虑延后达成。', 'factIds': ['f1']})
        self.assertTrue(validate_draft(report, data))
        self.assertEqual(validate_draft(draft('{{j1}}', nextStep={'text': '继续核查保护项以外的开销及可实现增收。', 'factIds': ['f1']}), data), [])

    def test_one_sentence_request_does_not_accept_extra_reason_paragraphs(self):
        report = draft(reasons=[{'text': '还有额外分析。', 'factIds': ['f1']}])
        self.assertTrue(validate_draft(report, context(question='一句话给结论，别堆数据')))
        self.assertEqual(context(question='不要只给结论，详细解释').char_limit, 3200)

    def test_decision_cannot_be_reversed_by_citing_a_non_anchor_fact_or_synonym(self):
        data = context(facts=[fact('f1', tool='purchase'), fact('f2', tool='purchase')], findings={
            'g1': {'tool': 'purchase', 'scope': '个人版', 'states': {
                'sufficientData': True, 'coversPrice': True, 'coversPriceAndReserve': False}}})
        for text in ['放心买，完全不会影响生活。', '资金绰绰有余，可以直接下单，不必担心储备。']:
            report = draft('{{j1}}', reasons=[{'text': text, 'factIds': ['f2']}])
            self.assertTrue(validate_draft(report, data), text)

    def test_historical_net_is_not_current_cash_even_when_value_is_owned(self):
        data = context(facts=[fact('f1', tool='cashflow', values=[
            {'label': '完整月份月均结余', 'value': 2300, 'unit': '元'}])],
            findings={'g1': {'tool': 'cashflow', 'scope': '个人版', 'states': {}}})
        self.assertTrue(validate_draft(draft('目前可动用余额为{{v1}}，可以据此安排付款。'), data))

    def test_single_book_and_month_cannot_be_relabelled(self):
        data = context(facts=[fact('f1', tool='trends', scope='家庭版', month='2026-10',
                                  values=[{'label': '已记收入', 'value': 8400, 'unit': '元'}])],
            findings={'g1': {'tool': 'trends', 'scope': '家庭版', 'states': {}}})
        data.payload['asOfDate'] = '2026-11-20'
        self.assertTrue(validate_draft(draft('个人版本月收入为{{v1}}。'), data))
        self.assertTrue(validate_draft(draft('家庭版本月收入为{{v1}}。'), data))

    def test_chinese_multipliers_percentages_and_time_need_references_independently(self):
        for text in ['收入翻三倍。', '未来收入增加百分之六十。', '这笔收入三天内到账。']:
            self.assertTrue(validate_draft(draft(text), context()), text)

    def test_priority_fallback_keeps_the_missing_purchase_balance(self):
        data = context(facts=[fact('f1', text='家庭可动用余额未知。', tool='purchase', scope='家庭版')],
            findings={'g1': {'tool': 'purchase', 'scope': '家庭版', 'states': {'sufficientData': False}}})
        data.payload['reviewPriorities'] = [{'title': '已记收支', 'reason': '历史收支存在需要核对的波动。', 'factIds': ['f1']}]
        self.assertIn('余额未知', fallback_report(data))

    def test_one_sentence_runway_and_savings_leads_contain_the_calculated_time(self):
        cases = [('runway', 'runwayMonths', 3.85,
                  {'sufficientData': True, 'hasDeficit': True}, '当前情景有月度资金缺口，覆盖时长按余额除以缺口计算。'),
                 ('savings', 'estimatedMonthsToTarget', 3,
                  {'sufficientData': True, 'meetsDeadline': None}, '未指定截止期限，若当前情景持续，可以估计达标所需时间。')]
        for tool, label, value, states, observation in cases:
            data = context(facts=[fact('f1', tool=tool, values=[{'label': label, 'value': value, 'unit': '个月'}])],
                findings={'g1': {'tool': tool, 'scope': '个人版', 'states': states, 'observations': [observation]}},
                question='一句话告诉我多久')
            self.assertIn(str(value), render_report(draft('{{j1}}'), data))

    def test_postposed_labels_and_comma_separated_book_labels_cannot_retype_values(self):
        net = context(facts=[fact('f1', values=[{'label': '月均结余', 'value': 2300, 'unit': '元'}])])
        self.assertTrue(validate_draft(draft('{{v1}}就是你现在的可动用余额。'), net))
        family = context(facts=[fact('f1', scope='家庭版', month='2026-10',
            values=[{'label': '已记收入', 'value': 900, 'unit': '元'}])],
            findings={'g1': {'tool': 'changes', 'scope': '家庭版', 'states': {}}})
        for text in ['个人版，收入合计为{{v1}}。', '{{v1}}是个人版的本月收入。']:
            with self.subTest(text=text): self.assertTrue(validate_draft(draft(text), family))
        self.assertEqual(validate_draft(draft('家庭版，已记收入为{{v1}}。'), family), [])

    def test_calculated_cash_deficit_and_positive_gap_magnitude_keep_their_roles(self):
        flow = context(facts=[fact('f1', values=[{'label': 'monthlyDeficit', 'value': 2600, 'unit': '元'}])])
        self.assertEqual(validate_draft(draft('收入全部停止时，估算每月现金缺口为{{v1}}。'), flow), [])
        reserve = context(facts=[fact('f1', values=[{'label': 'cashAfterPurchase', 'value': 4000, 'unit': '元'},
            {'label': 'reserveMargin', 'value': -1000, 'unit': '元'}])])
        self.assertEqual(validate_draft(draft('购买后剩余{{v1}}，比应急储备底线少{{v3}}。'), reserve), [])
        self.assertEqual(validate_draft(draft('若还要留足储备，资金共缺{{v3}}。'), reserve), [])

    def test_financial_assurance_paraphrases_cannot_reverse_immutable_judgment(self):
        data = context(facts=[fact('f1', tool='purchase')], findings={
            'g1': {'tool': 'purchase', 'scope': '个人版', 'states': {
                'sufficientData': True, 'coversPrice': True, 'coversPriceAndReserve': False}}})
        for text in ['当前储备底线已经满足。', '购买后生活开销不受影响。', '这笔购买完全负担得起。', '不能确定会不会影响生活，所以可以直接下单。', '不能保证但可以直接下单。']:
            with self.subTest(text=text):
                report = draft('{{j1}}', reasons=[{'text': text, 'factIds': ['f1']}])
                self.assertTrue(validate_draft(report, data))
        self.assertEqual(validate_draft(draft('{{j1}}', reasons=[{'text': '若还要留足储备，需要调整购买时间。', 'factIds': ['f1']}]), data), [])

    def test_quantity_bearing_original_remark_has_an_inert_label_reference(self):
        original = fact('f1', text='同期医疗支出发生变化，备注为一次就诊。')
        original['literals'] = ['一次就诊']
        data = context(facts=[original])
        label = next((v for v in data.values if v['unit'] == 'label' and v['value'] == '一次就诊'), None)
        self.assertIsNotNone(label)
        report = draft('备注为{{' + label['id'] + '}}，不能据此认定今后还会发生。')
        self.assertEqual(validate_draft(report, data), [])

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
