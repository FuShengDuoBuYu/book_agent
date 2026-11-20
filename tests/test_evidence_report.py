import unittest

from app.agent.evidence_report import build_facts, render_report, validate_draft
from app.schemas.report import ReportDraft


class EvidenceFactTests(unittest.TestCase):
    def test_supplemental_investigation_does_not_add_mandatory_answer_objectives(self):
        facts = build_facts([{'tool': 'purchase', 'supplemental': True, 'evidence': {
            'sufficientData': False, 'missingInputs': ['startingBalance']}}])
        self.assertTrue(facts)
        self.assertTrue(all(not row['required'] for row in facts))

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
        monthly = next(f for f in facts if f['binding']['tool'] == 'trends' and '已记收入' in f['text'])
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
        subtotals = {f['binding']['member']: next(v['value'] for v in f['values']
                     if v['label'] == '该成员查询期收入小计') for f in facts if f['id'] in required}
        self.assertEqual(subtotals, {'甲': 11700, '乙': 2550})

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

    def test_protected_expenses_leave_a_concrete_next_step_when_no_cut_is_identified(self):
        from datetime import date
        from app.tools.financial_analysis import run_analysis
        from tests.test_decision_analysis import ledger
        evidence = run_analysis('optimization', ledger(), as_of=date(2026, 11, 13),
                                protected_categories=['房租', '餐饮'])
        facts = build_facts([{'tool': 'optimization', 'evidence': evidence}])
        self.assertTrue(any(fact['required'] and '增收' in fact['text'] and '保护项' in fact['text'] for fact in facts))

    def test_positive_reserve_margin_cannot_be_reported_as_a_funding_gap(self):
        facts = build_facts([{'tool': 'purchase', 'evidence': {'sufficientData': True,
            'startingBalance': 12000, 'purchaseAmount': 6000, 'cashAfterPurchase': 6000,
            'reserveAmount': 5000, 'reserveMargin': 1000}}])
        self.assertTrue(any(f['required'] and '不存在这两项的资金缺口' in f['text'] for f in facts))

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

    def test_forecast_component_uses_the_computed_target_month(self):
        from datetime import date
        from app.tools.financial_analysis import run_analysis
        from tests.test_decision_analysis import ledger

        evidence = run_analysis('forecast', ledger(), as_of=date(2026, 11, 19),
                                metric='income', target_month='2027-01')
        facts = build_facts([{'tool': 'forecast', 'evidence': evidence}])
        component = next(row for row in facts if row['binding'].get('source') and not row['required'])
        self.assertEqual(component['binding']['targetMonth'], '2027-01')
        self.assertTrue(any(row['required'] and '2027-01' in row['text'] for row in facts))

    def test_projection_binds_the_calculated_horizon_and_user_changes(self):
        from datetime import date
        from app.tools.financial_analysis import run_analysis
        from tests.test_decision_analysis import ledger

        evidence = run_analysis('projection', ledger(), as_of=date(2026, 11, 19), months=3,
                                target_month='2026-12', changes=[{'metric': 'income', 'source': '家教', 'percent': -50}])
        facts = build_facts([{'tool': 'projection', 'evidence': evidence}])
        binding = facts[0]['binding']
        self.assertEqual((binding['startMonth'], binding['endMonth'], binding['months']),
                         ('2026-12', '2027-02', 3))
        self.assertTrue(any(row['required'] and '家教' in row['text'] and '-50' in row['text'] for row in facts))


class GroundedReportValidationTests(unittest.TestCase):
    def test_both_books_keep_their_own_fixed_decisions(self):
        from tests.test_report_composition import context, draft, fact
        facts = [fact('f1', tool='purchase', scope='家庭版'),
                 fact('f2', tool='purchase', scope='个人版', group='g2')]
        findings = {'g1': {'tool': 'purchase', 'scope': '家庭版', 'states': {'sufficientData': False}},
                    'g2': {'tool': 'purchase', 'scope': '个人版', 'states': {
                        'sufficientData': True, 'coversPrice': True, 'coversPriceAndReserve': True}}}
        data = context(facts=facts, findings=findings)
        report = draft('{{j1}} {{j2}}', ('f1', 'f2'))
        self.assertEqual(validate_draft(report, data), [])
        self.assertIn('【家庭版】', render_report(report, data))
        self.assertIn('【个人版】', render_report(report, data))
        self.assertTrue(validate_draft(draft('{{j2}}', ('f2',)), data))

    def test_unknown_references_cannot_be_rendered_as_evidence(self):
        from tests.test_report_composition import context, draft
        self.assertTrue(validate_draft(draft('依据已核对。', ('fabricated',)), context()))

    def test_misbound_finding_cannot_authorize_a_decision(self):
        from tests.test_report_composition import context, fact
        with self.assertRaisesRegex(ValueError, '范围'):
            context(facts=[fact('f1', tool='purchase', scope='家庭版')], findings={
                'g1': {'tool': 'purchase', 'scope': '个人版', 'states': {'coversPrice': True}}})

    def test_literal_variable_like_ledger_name_is_not_a_quantity_reference(self):
        from tests.test_report_composition import context, draft, fact
        data = context(facts=[fact('f1', source='画稿{{v1}}',
                                  values=[{'label': '已记收入', 'value': 900, 'unit': '元'}])])
        label = next(v['id'] for v in data.values if v['unit'] == 'label')
        report = draft('原始来源{{' + label + '}}已记录{{v1}}，需要核对用途。')
        self.assertEqual(validate_draft(report, data), [])
        reply = render_report(report, data)
        self.assertIn('画稿{{v1}}', reply)
        self.assertIn('900 元', reply)


if __name__ == '__main__':
    unittest.main()
