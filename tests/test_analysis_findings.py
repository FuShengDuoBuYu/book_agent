import json
import unittest
from datetime import date

from app.agent.analysis_findings import build_analysis_findings
from app.tools.financial_analysis import run_analysis


def group(tool, **evidence):
    return build_analysis_findings([{'tool': tool, 'mode': '家庭版', 'evidence': evidence}])['g1']


class FindingTests(unittest.TestCase):
    def test_real_runway_names_operands_and_handles_no_deficit(self):
        records = [{'year': 2026, 'month': m, 'day': 1, 'money': value, 'costType': category}
                   for m in (8, 9, 10) for value, category in [(1000, '工资'), (-600, '房租')]]
        for change, deficit in [(-50, True), (-10, False)]:
            e = run_analysis('runway', records, as_of=date(2026, 11, 19), starting_balance=800,
                             changes=[{'metric': 'income', 'percent': change}])
            found = group('runway', **e)
            self.assertEqual(found['states']['hasDeficit'], deficit)
            obs = ''.join(found['observations'])
            self.assertIn('情景月支出减去情景月收入', obs)
            self.assertIn('非负', obs)
            if deficit:
                self.assertIn('可动用余额除以月缺口', obs)
            else:
                self.assertIn('不能据此认定资金可无限期维持', obs)

    def test_real_protected_expenses_and_projection_without_observed_changes(self):
        records = [{'year': 2026, 'month': m, 'day': 1, 'money': amount, 'costType': category}
                   for m in (8, 9, 10) for amount, category in [(1500, '工资'), (-100, '医疗'), (-60, '交通')]]
        today = date(2026, 11, 19)
        found = group('optimization', **run_analysis('optimization', records, as_of=today, monthly_improvement=70))
        self.assertFalse(found['states']['hasCandidates'])
        self.assertEqual(set(found['labels']['protected']), {'医疗', '交通'})
        self.assertIn('未识别', ''.join(found['observations']))
        self.assertNotIn('即使候选开销全部削减', ''.join(found['observations']))
        e = run_analysis('projection', records, as_of=today, months=3)
        projection = group('projection', **e)
        self.assertNotIn('observedExpenseChanges', projection['labels'])
        self.assertIn('工资', projection['labels']['incomeSources'])
        self.assertIn('发生概率或重要性', ''.join(projection['limitations']))
        records += [dict(r, costType='展览') for r in records if r['costType'] == '交通']
        found = group('optimization', **run_analysis('optimization', records, as_of=today, monthly_improvement=70))
        self.assertTrue(found['states']['hasCandidates'])
        self.assertEqual(found['labels']['candidates'], ['展览'])

    def test_quality_names_only_fields_measured_and_found_missing(self):
        records = [{'year': 2026, 'month': 10, 'day': 5, 'money': -100, 'costType': '医疗',
                    'orderRemark': '门诊'}]
        quality = group('data_quality', **run_analysis('data_quality', records, as_of=date(2026, 11, 19)))
        self.assertEqual(quality['labels']['missingFields'], ['支付方式'])
        self.assertEqual(quality['labels']['checkedFields'], ['支付方式', '分类'])
        self.assertNotIn('备注缺失', ' '.join(quality['observations']))
        records[0]['bankName'] = '现金'
        complete = group('data_quality', **run_analysis('data_quality', records, as_of=date(2026, 11, 19)))
        self.assertEqual(complete['labels']['missingFields'], [])

    def test_projection_retains_historical_drivers_and_binds_exact_decision_method(self):
        records = [{'year': 2026, 'month': m, 'day': day, 'money': amount, 'costType': cat,
                    'orderRemark': note} for m, cost in [(8, 100), (9, 100), (10, 1000)]
                   for day, amount, cat, note in [(2, 5000, '收入', '校稿'), (3, -cost, '医疗', '门诊')]]
        kwargs = {'as_of': date(2026, 11, 19), 'months': 3, 'starting_balance': 0, 'target_amount': 8000}
        for tool in ('projection', 'scenario', 'savings'):
            change = [{'metric': 'income', 'source': '校稿', 'percent': -10}] if tool == 'scenario' else []
            found = group(tool, **run_analysis(tool, records, changes=change, **kwargs))
            self.assertIn('中位数', found['labels']['baselineMethod'])
            if tool == 'projection':
                self.assertEqual(found['labels']['incomeSources'], ['校稿'])
                self.assertEqual(found['labels']['observedExpenseChanges'], [{'category': '医疗', 'direction': '上升'}])
                self.assertIn('医疗', ''.join(found['observations']))
        budget = group('budget', **run_analysis('budget', records, budget_amount=900, **kwargs))
        self.assertNotIn('baselineMethod', budget['labels'])

    def test_recorded_peak_weekdays_and_payment_methods_remain_tied_within_their_period(self):
        records = [{'year': 2026, 'month': 10, 'day': day, 'money': -amount, 'bankName': bank,
                    'costType': '票券'} for day, amount, bank in [(5, 120, '现金'), (6, 120, '借记卡'),
                                                            (7, 20, '现金'), (13, 400, '借记卡')]]
        spec = {'groupBy': ['weekday', 'bank'], 'periods': [
            {'label': '前段', 'startDate': '2026-10-05', 'endDate': '2026-10-11'},
            {'label': '后段', 'startDate': '2026-10-12', 'endDate': '2026-10-18'}]}
        result = run_analysis('aggregate', records, as_of=date(2026, 11, 19), metric='expense', aggregate=spec)
        found = group('aggregate', **result)
        first, last = found['labels']['leadingGroups']
        self.assertEqual(first['period'], '前段')
        self.assertEqual(first['groups'], [{'weekday': '星期一', 'bank': '现金'}, {'weekday': '星期二', 'bank': '借记卡'}])
        self.assertTrue(first['tied'])
        self.assertFalse(first['visibleOnly'])
        self.assertEqual((last['period'], last['groups'], last['tied']), ('后段', [{'weekday': '星期二', 'bank': '借记卡'}], False))
        self.assertIn('并列', ''.join(found['observations']))
        self.assertNotIn('120', str(found))

    def test_aggregate_rank_restricts_partial_or_truncated_evidence_and_handles_no_matches(self):
        records = [{'year': 2026, 'month': 10, 'day': day, 'money': value, 'bankName': bank, 'costType': '服务'}
                   for day, value, bank in [(1, -100, '现金'), (3, 40, '借记卡'), (7, 20, '钱包')]]
        spec = {'groupBy': ['bank'], 'periods': [{'label': '查询期', 'startDate': '2026-10-01', 'endDate': '2026-10-31'}]}
        def find(spec, metric='net', ranges=None):
            return group('aggregate', **run_analysis('aggregate', records, as_of=date(2026, 11, 19), metric=metric,
                                                     aggregate=spec, queried_intervals=ranges))
        self.assertEqual(find(spec)['labels']['leadingGroups'][0]['groups'], [{'bank': '借记卡'}])
        for found in (find({**spec, 'limit': 2}), find(spec, ranges=[{'startDate': '2026-10-01', 'endDate': '2026-10-05'}])):
            self.assertTrue(found['labels']['leadingGroups'][0]['visibleOnly'])
            self.assertIn('已展示', ''.join(found['observations']))
        empty = find({**spec, 'filters': {'includeCategories': ['展览']}})
        self.assertFalse(empty['labels'].get('leadingGroups'))
        self.assertIn('没有可排名', ''.join(empty['observations']))
        all_metrics = {r['metric']: r['groups'] for r in find(spec, metric='all')['labels']['leadingGroups']}
        self.assertEqual(all_metrics, {'income': [{'bank': '借记卡'}], 'expense': [{'bank': '现金'}], 'net': [{'bank': '借记卡'}]})

    def test_real_projection_horizon_and_assumptions_and_goal_tools(self):
        today = date(2026, 11, 19)
        for expense, effect in [(50, '累计结余增加'), (100, '累计结余不变'), (150, '累计缺口扩大')]:
            records = [{'year': 2026, 'month': month, 'day': 1, 'money': amount, 'costType': category,
                        'orderRemark': remark} for month in (8, 9, 10)
                       for amount, category, remark in [(200, '收入', '画册'), (-expense, '展览', '门票')]]
            kwargs = {'as_of': today, 'months': 4, 'changes': [{'metric': 'income', 'source': '画册', 'percent': -50}]}
            projected = run_analysis('projection', records, **kwargs)
            for data in [projected, {k: v for k, v in projected.items() if k != 'assumptions'}]:
                f = build_analysis_findings([{'tool': 'projection', 'evidence': data}], {'previousMonths': 2})['g1']
                self.assertTrue(f['states']['deadlineExtended'])
                self.assertTrue(f['states']['assumptionsApplied'])
                self.assertIn(effect, ''.join(f['observations']))
                self.assertEqual(f['labels']['assumptions'][0]['source'], '画册')
        for tool in ('savings', 'optimization', 'sources', 'forecast'):
            data = run_analysis(tool, records, starting_balance=0, target_amount=800, **kwargs)
            finding = group(tool, **data)
            if tool == 'savings':
                self.assertFalse(finding['states']['meetsDeadline'])
            elif tool == 'optimization':
                self.assertIn('仍有缺口', ''.join(finding['observations']))
            else:
                self.assertIn('画册', [r['source'] for r in finding['labels']['sources']])

    def test_real_cashflow_structure_and_changes_notes_distinguish_missing_history(self):
        today = date(2026, 11, 19)
        records = [{'year': 2026, 'month': m, 'day': index+1, 'money': value, 'costType': category,
                    'orderRemark': remark} for index, (m, value, category, remark) in enumerate([
                        (9, 900, '收入', '画册'), (9, -180, '展览', '秋展'), (10, 900, '收入', '画册'),
                        (10, -360, '展览', '展览票券'), (10, -80, '展览', '票价80元')])]
        cash = group('cashflow', **run_analysis('cashflow', records, as_of=today))
        self.assertEqual(cash['labels']['incomeSources'], ['画册'])
        self.assertEqual(cash['labels']['expenseCategories'], ['展览'])
        changed = group('changes', **run_analysis('changes', records, as_of=today))
        self.assertEqual(changed['labels']['leadingChanges'][0]['afterRemarks'], ['展览票券'])
        for data in ([], [dict(records[0], month=11)]):
            cash = group('cashflow', **run_analysis('cashflow', data, as_of=today))
            self.assertNotIn('月均已记结余', ''.join(cash['observations']))

    def test_existing_category_increase_is_not_new_spending(self):
        data = {'category': '展览', 'metric': 'expense', 'before': 180, 'after': 440, 'change': 260}
        for finding in (group('aggregate', comparisons=[data], comparisonDimensions=['category']),
                        group('changes', expenseDrivers=[dict(data, name='展览', beforeCount=1, afterCount=2,
                                                              meanAmountBefore=180, meanAmountAfter=220)])):
            self.assertIn('展览', ' '.join(finding['observations']))
            self.assertIn('前期已有', ' '.join(finding['observations']))
            self.assertNotIn('新增', ' '.join(finding['observations']))
            self.assertNotRegex(json.dumps(finding, ensure_ascii=False), r'\d')

    def test_savings_reachable_does_not_require_further_saving(self):
        found = group('savings', sufficientData=True, months=3, projectedBalance=1200, targetAmount=1000)
        self.assertTrue(found['states']['meetsDeadline'])
        self.assertIn('无需额外', ''.join(found['observations']))
        no_deadline = group('savings', sufficientData=True, months=None, estimatedMonthsToTarget=None,
                            baselineMonthlySaving=-120, remainingToTarget=500)
        self.assertIsNone(no_deadline['states']['meetsDeadline'])
        self.assertFalse(no_deadline['states']['finiteTimeToTarget'])

    def test_missing_balance_preserves_unknown_purchase_and_runway(self):
        for tool in ('purchase', 'runway'):
            finding = group(tool, sufficientData=False, missingInputs=['startingBalance'])
            self.assertIn('当前可动用余额', ''.join(finding['observations']))
            self.assertFalse(finding['states']['sufficientData'])

    def test_projection_distinguishes_applied_assumption_and_horizon(self):
        found = group('projection', sufficientData=True, monthlyNet=-80, totalNet=-240,
                      assumptions=[{'source': '课程', 'metric': 'income', 'totalChange': -300}])
        self.assertTrue(found['states']['assumptionsApplied'])
        self.assertIn('课程', json.dumps(found, ensure_ascii=False))
        self.assertIn('期限', ''.join(found['observations']))
        self.assertNotRegex(json.dumps(found, ensure_ascii=False), r'\d')

    def test_source_remarks_stay_attached_to_member_and_group(self):
        source = {'source': '画册', 'member': '阿青', 'metric': 'income', 'excluded': False,
                  'remarks': ['画册初排', '画册调版', '画册完稿'], 'amount': 500}
        excluded = dict(source, source='旧车出售', member='云川', excluded=True, remarks=[])
        for found in (group('sources', sources=[source, excluded]),
                      group('forecast', metric='income', sufficientData=True, sourceForecasts=[source, excluded])):
            first, second = found['labels']['sources']
            self.assertEqual((first['member'], first['source'], first['remarks']), ('阿青', '画册', source['remarks']))
            self.assertTrue(second['excludedFromRecurringIncome'])
            self.assertNotRegex(json.dumps(found, ensure_ascii=False), r'\d')

    def test_unknown_group_ids_and_bounds(self):
        findings = build_analysis_findings([{'tool': 'unknown', 'evidence': {}}, {'tool': 'cashflow', 'evidence': {}}])
        self.assertEqual(list(findings), ['g1', 'g2'])
        for found in findings.values():
            self.assertLessEqual(len(found['observations']), 6)
            self.assertLessEqual(len(found['limitations']), 4)
            self.assertTrue(found['limitations'])

    def test_none_zero_and_shortfall_have_distinct_decision_meanings(self):
        finding = group('purchase', sufficientData=True, cashAfterPurchase=800, reserveMargin=0)
        self.assertTrue(finding['states']['coversPriceAndReserve'])
        finding = group('optimization', sufficientData=True, monthlyImprovement=500,
                        reviewableMonthlySpend=100, gapBeyondReviewableSpend=400, protectedCategories=['房租'],
                        candidates=[{'category': '零食'}])
        self.assertIn('房租', finding['labels']['protected'])
        self.assertIn('仍有缺口', ''.join(finding['observations']))
        self.assertNotRegex(json.dumps(finding, ensure_ascii=False), r'\d')
        empty = group('budget', sufficientData=True, remaining=500, hasRecords=False)
        self.assertTrue(any('未见' in s for s in empty['limitations']))
        complete = group('cashflow', completeMonthAverage={'months': ['2026-10'], 'net': -35})
        self.assertIn('为负', ''.join(complete['observations']))
        self.assertNotIn('2026', str(complete))

    def test_growth_driver_is_proportional_to_change_not_the_largest_final_category(self):
        finding = group('changes', expenseChange=270, expenseDrivers=[
            {'name': '住宿', 'before': 900, 'after': 910, 'change': 10},
            {'name': '展览', 'before': 180, 'after': 440, 'change': 260}])
        self.assertEqual(finding['labels']['leadingChanges'][0]['label'], '展览')
        self.assertTrue(finding['labels']['leadingChanges'][0]['previouslyRecorded'])

    def test_majority_concentration_quality_clues_and_unsupported_are_bounded(self):
        stable = group('income_stability', sufficientData=True, incomeMin=80, incomeMax=90,
                       sources=[{'source': '课程', 'sharePercent': 55}])
        self.assertFalse(stable['states']['constantInSample'])
        self.assertEqual(stable['labels']['majoritySource'], '课程')
        quality = group('data_quality', possibleDuplicates=[], missingCategoryCount=4)
        self.assertIn('缺失字段', ''.join(quality['observations']))
        self.assertNotRegex(json.dumps([stable, quality], ensure_ascii=False), r'\d')
