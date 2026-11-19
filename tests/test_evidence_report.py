import unittest
from unittest.mock import AsyncMock, patch

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

    def test_same_unit_after_a_typed_variable_is_normalized_without_changing_its_value(self):
        facts = [{'id': 'f1', 'required': True, 'text': '历史样本入账 8,000 元。',
                  'values': [{'label': '样本', 'value': 8000, 'unit': '元'}]}]
        prepared, repairs = prepare_draft(self.draft(analysis='样本入账{{v1}}元，不能代表持续收入。'), facts)
        self.assertEqual(prepared.sections[0].analysis, '样本入账{{v1}}，不能代表持续收入。')
        self.assertEqual(repairs['normalizedUnits'], 1)
        with self.assertRaises(ValueError):
            prepare_draft(self.draft(analysis='样本记录{{v1}}天。'), facts)

    def test_declared_inline_evidence_reference_is_restored_before_validation(self):
        facts = [*self.facts, {'id': 'f2', 'required': False, 'text': '旧设备出售被剔除。'}]
        draft = self.draft(analysis='设备出售不属于持续入账（f2），需要核对后续来源安排。')
        prepared, repairs = prepare_draft(draft, facts)
        self.assertEqual(prepared.sections[0].factIds, ['f1', 'f2'])
        self.assertNotIn('(f2)', prepared.sections[0].analysis)
        self.assertNotIn('（f2）', prepared.sections[0].analysis)
        self.assertIn('f2', repairs['restoredReferences'])

    def test_forecast_child_evidence_supports_the_shared_target_date(self):
        from datetime import date
        from app.tools.financial_analysis import run_analysis
        from tests.test_decision_analysis import ledger
        evidence = run_analysis('forecast', ledger(), as_of=date(2026, 11, 19), metric='income', target_month='2027-01')
        facts = build_facts([{'tool': 'forecast', 'evidence': evidence}])
        child = next(row for row in facts if '条件估计' in row['text'] and not row['required'])
        draft = self.draft(title='2027-01 分项收入', factIds=[child['id']], analysis='先确认这些来源后续是否继续。')
        self.assertFalse([issue for issue in validate_draft(draft, facts) if '日期' in issue])

    def test_projection_calendar_includes_every_computed_month_across_year_boundary(self):
        from datetime import date
        from app.tools.financial_analysis import run_analysis
        from tests.test_decision_analysis import ledger
        evidence = run_analysis('projection', ledger(), as_of=date(2026, 11, 19), months=3, target_month='2026-12')
        facts = build_facts([{'tool': 'projection', 'evidence': evidence}])
        draft = self.draft(title='未来三个月', factIds=[row['id'] for row in facts if row['required']],
                           analysis='2026-12 至 2027-02 这三个月的结果以所列假设为前提。')
        self.assertEqual(validate_draft(draft, facts), [])

    def test_supplemental_investigation_does_not_add_mandatory_answer_objectives(self):
        facts = build_facts([{'tool': 'purchase', 'supplemental': True, 'evidence': {
            'sufficientData': False, 'missingInputs': ['startingBalance']}}])
        self.assertTrue(facts)
        self.assertTrue(all(not row['required'] for row in facts))

    def test_model_arithmetic_rejected_including_chinese_money(self):
        for prose in ('你还可支撑1.5个月。', '可存三千元。'):
            self.assertTrue(validate_draft(self.draft(analysis=prose), self.facts))

    def test_renderer_preserves_fact_labels(self):
        result = render_report(self.draft(), self.facts)
        self.assertIn(self.facts[0]['text'], result)

    def test_required_omission_cannot_make_an_unrelated_amount_claim_valid(self):
        facts = [{'id': 'f1', 'text': '房租查询期小计6000元。', 'required': False,
                  'values': [{'label': '房租小计', 'value': 6000, 'unit': '元'}]},
                 {'id': 'f2', 'text': '查询期结余3100元。', 'required': True,
                  'values': [{'label': '查询期结余', 'value': 3100, 'unit': '元'}]}]
        draft = self.draft(title='房租安排', factIds=['f1'], analysis='房租每月3100元。')
        with self.assertRaises(ValueError):
            prepare_draft(draft, facts)

    def test_report_must_answer_the_user_instead_of_describing_internal_fields(self):
        for text in ['根据用户指令，我需要解释 section.answerGoal 与 computedFacts 的关系。',
                     'answerGoal 是本报告的目标，factIds 是计算事实的来源。']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                prepare_draft(self.draft(analysis=text), self.facts)

    def test_cashflow_average_and_month_total_keep_distinct_typed_bindings(self):
        from datetime import date
        from app.tools.financial_analysis import run_analysis
        from tests.test_decision_analysis import ledger
        results = [{'tool': tool, 'mode': '个人版', 'metric': 'all',
                    'historyWindow': {'startDate': '2026-08-01', 'endDate': '2026-10-31'},
                    'evidence': run_analysis(tool, ledger(), as_of=date(2026, 11, 19))}
                   for tool in ('cashflow', 'trends')]
        facts = build_facts(results)
        average = next(f for f in facts if f['text'].startswith('【个人版】完整月份'))
        monthly = next(f for f in facts if '已记收入' in f['text'])
        self.assertEqual(average['binding']['aggregation'], 'complete_month_average')
        self.assertEqual(monthly['binding']['aggregation'], 'recorded_month_total')
        self.assertEqual(monthly['binding']['scope'], '个人版')
        self.assertIn('month', monthly['binding'])

    def test_missing_liquidity_is_a_mandatory_fact(self):
        facts = build_facts([{'tool': 'purchase', 'mode': '个人版', 'evidence': {
            'sufficientData': False, 'missingInputs': ['startingBalance'], 'purchaseAmount': 6000}}])
        self.assertTrue(any(f['required'] and '可动用余额' in f['text'] for f in facts))

    def test_family_income_subtotals_are_computed_evidence_for_the_answer(self):
        from datetime import date
        from app.tools.financial_analysis import run_analysis
        rows = [{'year': 2026, 'month': 10, 'day': 4, 'money': amount, 'costType': category,
            'orderRemark': remark, 'memberKey': member, 'memberName': name}
            for member, name, amount, category, remark in [('a', '甲', 3200, '收入', '编辑部月结'),
                ('a', '甲', 500, '稿费', '海报项目'), ('a', '甲', 8000, '其他', '旧设备出售'),
                ('b', '乙', 2100, '收入', '编辑部月结'), ('b', '乙', 450, '配送', '配送费')]]
        evidence = run_analysis('sources', rows, as_of=date(2026, 11, 19), catalog_metric='income')
        facts = build_facts([{'tool': 'sources', 'mode': '家庭版', 'evidence': evidence}])
        required = [f['id'] for f in facts if f['required']]
        self.assertEqual(len(required), 2)
        draft = self.draft(title='成员收入小计', factIds=required, analysis='甲已记录11700元，乙已记录2550元；历史入账不等于持续性收入。')
        self.assertEqual(validate_draft(draft, facts), [])

    def test_empty_source_query_has_a_required_unknown_result(self):
        facts = build_facts([{'tool': 'sources', 'evidence': {'sources': [], 'memberTotals': [], 'sourceCount': 0}}])
        self.assertTrue(any(f['required'] and '未知' in f['text'] for f in facts))

    def test_source_inspection_keeps_separate_original_remarks_in_the_model_view(self):
        remarks = ['星港海报初排', '星港海报调版', '星港海报完稿']
        facts = build_facts([{'tool': 'sources', 'metric': 'income', 'evidence': {'sources': [
            {'metric': 'income', 'member': '甲', 'source': '星港海报', 'amount': 3000,
             'recordCount': 6, 'remarks': remarks}]}}])
        self.assertEqual(facts[0]['binding']['originalRemarks'], remarks)
        self.assertIn('星港海报完稿', facts[0]['modelText'])

    def test_verified_scenario_inputs_can_be_repeated_without_requiring_a_separate_condition_paragraph(self):
        from datetime import date
        from app.tools.financial_analysis import run_analysis
        from tests.test_decision_analysis import ledger
        e = run_analysis('projection', ledger(), as_of=date(2026, 11, 19), months=3,
                         changes=[{'metric': 'income', 'source': '家教', 'percent': -50}])
        facts = build_facts([{'tool': 'projection', 'evidence': e}])
        draft = self.draft(factIds=[facts[0]['id']], analysis='在家教减半的假设下，这三个月的结果以所列条件延续为前提。')
        self.assertFalse([issue for issue in validate_draft(draft, facts) if '数量' in issue])

    def test_signed_amount_change_can_be_described_by_its_positive_reduction(self):
        from datetime import date
        from app.tools.financial_analysis import run_analysis
        from tests.test_decision_analysis import ledger
        e = run_analysis('projection', ledger(), as_of=date(2026, 11, 19), months=3,
                         changes=[{'metric': 'expense', 'amount': -150, 'count': 2}])
        facts = build_facts([{'tool': 'projection', 'evidence': e}])
        draft = self.draft(factIds=[facts[0]['id']], analysis='每次少花150元的假设已经计入推演。')
        self.assertFalse([issue for issue in validate_draft(draft, facts) if '数量' in issue])

    def test_required_evidence_can_fill_additional_sections_without_losing_the_model_answer(self):
        facts = [{'id': f'f{i}', 'text': f'已核对条件{"甲" * i}。', 'required': True} for i in range(1, 17)]
        draft = self.draft(analysis='应根据已核对条件逐项评估后续安排。')
        prepared, _ = prepare_draft(draft, facts)
        self.assertEqual({key for section in prepared.sections for key in section.factIds}, {f['id'] for f in facts})
        self.assertEqual(prepared.sections[0].analysis, draft.sections[0].analysis)
        self.assertEqual(validate_draft(prepared, facts), [])

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
        with self.assertRaises(ValueError):
            prepare_draft(draft, facts)

    def test_numeric_claim_requires_repair_before_any_section_is_sent(self):
        draft = ReportDraft.model_validate({'sections': [
            {'title': '结余测算', 'factIds': ['f1'], 'analysis': '预计新增12000元。'},
            {'title': '需确认的变化', 'factIds': ['f1'], 'analysis': '建议确认后续收入安排，再据实际到账情况调整计划。'}]})
        with self.assertRaises(ValueError):
            prepare_draft(draft, self.facts)

    def test_advice_does_not_turn_unknown_recurrence_into_a_fact(self):
        facts = [{'id': 'f1', 'required': True, 'text': '医疗记录备注：一次就诊。不能确认是否再次发生。'}]
        draft = self.draft(analysis='建议将这笔一次性支出从常规预算中剔除。若确认后续无需复诊，再单独评估预算。')
        with self.assertRaises(ValueError):
            prepare_draft(draft, facts)

    def test_no_identified_candidates_does_not_prove_no_adjustment_possible(self):
        facts = [{'id': 'f1', 'required': True, 'text': '可逐项核查的月度支出上界为 0 元；不能证明现实没有优化空间。'}]
        draft = self.draft(analysis='其他所有支出均无调整余地。常规支出已无进一步压缩的可能。建议核对未分类项目，并考虑增收或延长期限。')
        with self.assertRaises(ValueError):
            prepare_draft(draft, facts)

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
        with self.assertRaises(ValueError):
            prepare_draft(draft, self.facts)

    def test_source_labels_and_available_cash_do_not_guarantee_future_safety(self):
        draft = self.draft(analysis='固定工资通常具有较好的稳定性。若当前余额允许，这笔支出不会导致生活困难。近期支出增加并非日常消费习惯改变。请核对近期必付账单和应急储备。')
        with self.assertRaises(ValueError):
            prepare_draft(draft, self.facts)

    def test_positive_reserve_margin_cannot_be_reported_as_a_funding_gap(self):
        facts = build_facts([{'tool': 'purchase', 'evidence': {'sufficientData': True,
            'startingBalance': 12000, 'purchaseAmount': 6000, 'cashAfterPurchase': 6000,
            'reserveAmount': 5000, 'reserveMargin': 1000}}])
        self.assertTrue(any(f['required'] and '不存在这两项的资金缺口' in f['text'] for f in facts))
        draft = self.draft(title='资金缺口分析', analysis='需要收入及时到账以填补该缺口。请核对近期必付账单。')
        with self.assertRaises(ValueError):
            prepare_draft(draft, facts)

    def test_zero_duplicate_count_cannot_be_restated_as_detected_duplicates(self):
        facts = [{'id': 'f1', 'required': True, 'text': '检测到疑似重复组 0 组。'}]
        draft = self.draft(analysis='系统检测到疑似重复组，且医疗类别样本不足。建议核对交易原始凭据。')
        with self.assertRaises(ValueError):
            prepare_draft(draft, facts)

    def test_no_duplicate_finding_is_not_rejected_as_a_positive_claim(self):
        facts = [{'id': 'f1', 'required': True, 'text': '检测到疑似重复组 0 组。'}]
        for wording in ['没有检测到疑似重复组，但还需核对凭据。', '未发现重复记录，不能证明没有记账错误。']:
            with self.subTest(wording=wording):
                prepared, _ = prepare_draft(self.draft(analysis=wording), facts)
                self.assertEqual(prepared.sections[0].analysis, wording)

    def test_filtered_period_totals_are_available_without_model_arithmetic(self):
        from app.tools.financial_analysis import run_analysis
        from tests.test_aggregate_analysis import TODAY, period, row
        records = [row('2026-09-04', -220, '餐饮'), row('2026-09-05', -180, '展览'),
                   row('2026-10-02', -220, '餐饮'), row('2026-10-03', -440, '展览')]
        e = run_analysis('aggregate', records, as_of=TODAY, metric='expense', aggregate={
            'periods': [period('九月', '2026-09-01', '2026-09-30'), period('十月', '2026-10-01', '2026-10-31')],
            'groupBy': ['month', 'category'], 'limit': 1})
        facts = build_facts([{'tool': 'aggregate', 'evidence': e}])
        totals = [f for f in facts if any(v['label'] == '区间支出小计' for v in f['values'])]
        self.assertEqual([next(v['value'] for v in f['values'] if v['label'] == '区间支出小计') for f in totals], [400, 660])
        self.assertTrue(any(any(v['label'] == '区间总额变化' and v['value'] == 260 for v in f['values']) for f in facts))
        draft = self.draft(factIds=[f['id'] for f in totals], analysis='筛选后支出从400元变为660元。')
        prepared, _ = prepare_draft(draft, facts)
        self.assertIn('400元', prepared.sections[0].analysis)

    def test_neutral_gap_heading_and_explicit_no_gap_are_not_positive_gap_claims(self):
        facts = build_facts([{'tool': 'purchase', 'evidence': {'sufficientData': True,
            'startingBalance': 12000, 'purchaseAmount': 6000, 'cashAfterPurchase': 6000,
            'reserveAmount': 5000, 'reserveMargin': 1000}}])
        draft = self.draft(title='资金缺口分析', analysis='购买和储备可以同时覆盖，不存在资金缺口。还需核对其他近期必付账单。')
        prepared, _ = prepare_draft(draft, facts)
        self.assertIn('不存在资金缺口', prepared.sections[0].analysis)

    def test_action_before_an_unverified_numeric_consequence_is_preserved(self):
        draft = self.draft(analysis='若收入中断，需尽快确认新的收入来源或调整非必要支出，否则资金将在不到四个月耗尽。')
        with self.assertRaises(ValueError):
            prepare_draft(draft, self.facts)


class EvidenceReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_runs_outline_bound_comment_and_global_review(self):
        from app.agent.report_composition import ReportOutline, ReportComment
        from app.schemas.report import ReportReview
        reporter = EvidenceReporter()
        reporter._invoke = AsyncMock(side_effect=[
            ReportOutline(sections=[{'title': '条件与方案', 'answerGoal': '购买会影响生活吗？', 'factIds': ['f1']}]),
            ReportComment(analysis='需先确认当前可动用余额和近期必付账单，再判断购买影响。'),
            ReportReview(supported=True, completed=True, issues=[])])
        reply, result = await reporter.answer('购买会影响生活吗？', {}, [{'tool': 'purchase', 'evidence': {
            'sufficientData': False, 'missingInputs': ['startingBalance']}}])
        self.assertEqual(result['status'], 'reviewed')
        self.assertIn('需先确认当前可动用余额', reply)
        self.assertEqual([c.args[0].__name__ for c in reporter._invoke.call_args_list],
                         ['ReportOutline', 'ReportComment', 'ReportReview'])
        self.assertEqual(len(result['attempts'][0]['calls']['composition']), 2)

    async def test_each_rejected_section_has_its_own_complete_repair_diagnostic(self):
        reporter = EvidenceReporter()
        bad = ReportDraft.model_validate({'sections': [
            {'title': '第一处', 'factIds': ['f1'], 'analysis': '请确认用途。' * 70 + '可以撑999个月。'},
            {'title': '第二处', 'factIds': ['f1'], 'analysis': '能多花888元。'}]})
        good = ReportDraft.model_validate({'sections': [
            {'title': '核查条件', 'factIds': ['f1'], 'analysis': '需先提供当前可动用余额和近期必付账单，才能评估购买影响。'}]})
        reporter._draft = AsyncMock(side_effect=[bad, good])
        reporter._review = AsyncMock(return_value=[])
        _, result = await reporter.answer('购买后生活受影响吗？', {}, [{'tool': 'purchase', 'evidence': {
            'sufficientData': False, 'missingInputs': ['startingBalance']}}])
        corrections = reporter._draft.call_args_list[1].args[0]['corrections']
        self.assertEqual(len(corrections), 2)
        self.assertIn('第1段', corrections[0])
        self.assertIn('999个月', corrections[0])
        self.assertIn('第2段', corrections[1])
        self.assertIn('888元', corrections[1])
        self.assertEqual(result['status'], 'reviewed')

    async def test_completeness_review_receives_uncited_available_evidence(self):
        reporter = EvidenceReporter()
        reporter._draft = AsyncMock(return_value=ReportDraft.model_validate({'sections': [
            {'title': '固定开销', 'factIds': ['f1'], 'analysis': '请确认后续安排。'}]}))
        reporter._review = AsyncMock(return_value=[])
        rows = [{'source': name, 'activeMonths': 2, 'transactions': 2, 'typicalAmount': 100}
                for name in ('租房开支', '交通定期充值')]
        await reporter.answer('列出重复支出', {}, [{'tool': 'recurring', 'evidence': {'expense': rows}}])
        review = reporter._review.call_args.args[0]
        available = review['facts'] + review['report']['displayedFacts']
        self.assertEqual({fact['id'] for fact in available}, {'f1', 'f2'})
        self.assertIn('交通定期充值', str(review))

    async def test_fallback_preserves_every_required_fact(self):
        reporter = EvidenceReporter()
        reporter._draft = AsyncMock(side_effect=TimeoutError)
        reporter._review = AsyncMock(return_value=[])
        facts = [{'id': f'f{i}', 'text': f'必要条件甲{"乙" * i}。', 'required': True} for i in range(1, 14)]
        with patch('app.agent.evidence_report.build_facts', return_value=facts):
            reply, result = await reporter.answer('逐项核查必要条件', {}, [])
        self.assertEqual(result['status'], 'computed_fallback')
        self.assertIn(facts[-1]['text'], reply)

    async def test_other_scope_forecast_does_not_hide_explicit_recurring_expenses(self):
        from tests.test_analysis_artifacts import forecast_result
        reporter = EvidenceReporter()
        reporter._draft = AsyncMock(side_effect=TimeoutError)
        rows = [{'source': '租房开支', 'activeMonths': 2, 'transactions': 2, 'typicalAmount': 100}]
        forecast = forecast_result()
        forecast['mode'] = '家庭版'
        _, result = await reporter.answer('个人固定开销和家庭收入预测', {}, [forecast,
            {'tool': 'recurring', 'mode': '个人版', 'metric': 'expense', 'evidence': {'expense': rows}}])
        self.assertIn('租房开支', str(result['facts']))

    async def test_numeric_restatement_repaired_before_qualitative_review(self):
        reporter = EvidenceReporter.__new__(EvidenceReporter)
        draft = ReportDraft.model_validate({'sections': [{'title': '购买影响', 'factIds': ['f1'],
            'analysis': '你可以支撑1.5个月。先核对近期必付账单，再决定是否购买。'}]})
        reporter._draft = AsyncMock(side_effect=[draft, ReportDraft.model_validate({'sections': [{'title': '购买影响', 'factIds': ['f1'], 'analysis': '先核对近期必付账单，再决定是否购买。'}]})])
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
        self.assertEqual(validation['status'], 'computed_fallback')
        self.assertIn('可动用余额', reply)
