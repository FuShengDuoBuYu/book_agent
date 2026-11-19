import unittest
from datetime import date
from json import dumps

from app.agent import evidence_report
from app.agent.artifacts import build_artifacts, render_artifacts_markdown
from app.tools.aggregate_analysis import aggregate_analysis


def forecast_result():
    return {'mode': '家庭版', 'tool': 'forecast', 'evidence': {
        'metric': 'income', 'targetMonth': '2026-12', 'sufficientData': True,
        'estimate': 7100, 'lower': 6500, 'upper': 7500,
        'baselineMonths': ['2026-08', '2026-09', '2026-10'],
        'method': '按来源计算历史基准，再汇总。', 'excludedIncomeTotal': 0,
        'sourceForecasts': [
            {'sourceKey': 's1', 'source': '带球', 'member': '成员甲', 'estimate': 1200,
             'lower': 800, 'upper': 1600, 'remarks': ['周末带球'], 'excluded': False},
            {'sourceKey': 's2', 'source': '实习', 'member': '成员乙', 'estimate': 5000,
             'lower': 5000, 'upper': 5000, 'remarks': ['实习工资'], 'excluded': False},
            {'sourceKey': 's3', 'source': '实验室补贴', 'member': '成员甲', 'estimate': 900,
             'lower': 700, 'upper': 900, 'remarks': ['助研补贴'], 'excluded': False},
        ]}}


def trends_result():
    return {'tool': 'trends', 'mode': '个人版', 'evidence': {'months': [
        {'month': '2026-08', 'incomeTotal': 6000, 'expenseTotal': 200, 'netIncomeMinusExpense': 5800, 'partial': False},
        {'month': '2026-10', 'incomeTotal': 7000, 'expenseTotal': 900, 'netIncomeMinusExpense': 6100, 'partial': False},
    ], 'coverageNote': '缺失月份未知。'}}


def expense_forecast():
    return {'tool': 'forecast', 'mode': '个人版', 'evidence': {
        'metric': 'expense', 'sufficientData': True, 'targetMonth': '2026-12',
        'estimate': 800, 'lower': 600, 'upper': 900,
    }}


def aggregate_result(records, *, metric='all', queried_intervals=None, **spec):
    return {'tool': 'aggregate', 'mode': '家庭版', 'evidence': aggregate_analysis(
        records, as_of=date(2026, 11, 19), metric=metric, aggregate=spec, queried_intervals=queried_intervals,
    )}


def aggregate_order(year, amount, *, member_key='opaque-member-a', category='交通'):
    return {'year': year, 'month': 1, 'day': 2, 'money': amount, 'costType': category,
            'memberKey': member_key, 'memberName': '同名成员'}


class ArtifactTests(unittest.TestCase):
    def test_forecast_evidence_keeps_method_and_member_source_breakdown(self):
        facts = evidence_report.build_facts([forecast_result()])
        text = '\n'.join(row['text'] for row in facts)
        self.assertIn('按来源', text)
        self.assertIn('带球', text)
        self.assertIn('成员乙', text)

    def test_forecast_table_total_reconciles_and_is_downloadable(self):
        artifacts = build_artifacts([forecast_result()], {'table': True, 'metric': 'income'})
        table = next(row for row in artifacts if row['kind'] == 'table')
        self.assertEqual([row['estimate'] for row in table['rows']], [1200, 5000, 900, 7100])
        self.assertIn('成员乙', render_artifacts_markdown(artifacts))

    def test_income_chart_does_not_substitute_expense_and_preserves_unknown_month(self):
        artifacts = build_artifacts([trends_result()], {'chart': 'line', 'metric': 'income'},
                                    window={'startDate': '2026-08-01', 'endDate': '2026-10-31'})
        chart = next(row for row in artifacts if row['kind'] == 'line')
        self.assertEqual(chart['series'], [{'key': 'income', 'label': '收入'}])
        self.assertEqual([row['income'] for row in chart['rows']], [6000, None, 7000])
        self.assertNotIn('expense', chart['rows'][0])

    def test_trend_notes_explain_missing_and_partial_months_in_shown_metric_and_period(self):
        result = trends_result()
        months = result['evidence']['months']
        months[-1]['partial'] = True
        months.append({**months[-1], 'month': '2026-11'})
        result['evidence']['coverageNote'] = '2026-11 尚未结束，无法判断支出是否回落。'
        chart = build_artifacts([result], {'chart': 'line', 'metric': 'income'},
                                window={'startDate': '2026-08-01', 'endDate': '2026-10-15'})[0]
        notes = '\n'.join(chart['notes'])
        self.assertIn('2026-09', notes)
        self.assertIn('2026-10', notes)
        self.assertIn('收入', notes)
        self.assertNotIn('支出', notes)
        self.assertNotIn('2026-11', notes)

    def test_export_only_includes_downloadable_forecast_dataset(self):
        artifacts = build_artifacts([expense_forecast()], {
            'export': True, 'table': False, 'chart': 'none', 'metric': 'expense',
        })
        self.assertEqual([item['kind'] for item in artifacts], ['table'])
        self.assertEqual(artifacts[0]['rows'], [{'month': '2026-12', 'estimate': 800, 'lower': 600, 'upper': 900}])

    def test_export_only_trends_use_a_table_without_selecting_a_chart(self):
        artifacts = build_artifacts([trends_result()], {
            'export': True, 'table': False, 'metric': 'income', 'includeTrends': True,
        })
        self.assertEqual([item['kind'] for item in artifacts], ['table'])
        self.assertEqual([row['income'] for row in artifacts[0]['rows']], [6000, None, 7000])

    def test_forecast_chart_plots_requested_future_estimate_not_historical_trends(self):
        artifacts = build_artifacts([trends_result(), expense_forecast()], {
            'chart': 'bar', 'chartTarget': 'forecast', 'metric': 'expense', 'includeTrends': False,
        })
        self.assertEqual([item['kind'] for item in artifacts], ['bar'])
        self.assertEqual(artifacts[0]['xKey'], 'month')
        self.assertEqual(artifacts[0]['series'], [{'key': 'estimate', 'label': '支出条件估计'}])
        self.assertEqual(artifacts[0]['rows'], [{'month': '2026-12', 'estimate': 800, 'lower': 600, 'upper': 900}])

    def test_projection_chart_plots_each_requested_future_month_and_metric(self):
        result = {'tool': 'projection', 'mode': '个人版', 'evidence': {
            'sufficientData': True, 'startMonth': '2026-12', 'months': 3,
            'monthlyIncome': 800, 'monthlyExpense': 300, 'monthlyNet': 500,
        }}
        artifacts = build_artifacts([trends_result(), result], {
            'chart': 'line', 'chartTarget': 'projection', 'metric': 'net',
        })
        self.assertEqual([item['kind'] for item in artifacts], ['line'])
        self.assertEqual(artifacts[0]['series'], [{'key': 'net', 'label': '结余'}])
        self.assertEqual([(row['month'], row['net']) for row in artifacts[0]['rows']],
                         [('2026-12', 500), ('2027-01', 500), ('2027-02', 500)])
        self.assertNotIn('expense', artifacts[0]['rows'][0])

    def test_forecast_lineage_keeps_all_stage_remarks_and_reports_evidence_limits(self):
        stages = ['星港海报初排', '星港海报调版', '星港海报完稿']
        forecast = forecast_result()
        component = forecast['evidence']['sourceForecasts'][0]
        component.update(source='星港项目', remarks=stages)
        sources = {'tool': 'sources', 'mode': '家庭版', 'evidence': {
            'sources': [{**component, 'metric': 'income', 'amount': 3000, 'recordCount': 6}],
            'omittedSourceCount': 12,
        }}
        artifacts = build_artifacts([forecast, sources], {
            'metric': 'income', 'includeLineage': True, 'includeSources': True,
        })
        self.assertEqual(len(artifacts), 2)
        forecast_table, lineage = artifacts
        for remark in stages:
            self.assertIn(remark, forecast_table['rows'][0]['basis'])
        self.assertEqual(lineage['rows'], [{'remark': remark, 'member': '成员甲', 'source': '星港项目',
                                           'direction': '收入'} for remark in stages])
        self.assertTrue(any('4 条' in note and '160 字' in note for note in lineage['notes']))
        self.assertTrue(any('12' in note and '来源' in note for note in lineage['notes']))

    def test_primary_sources_publish_individual_remarks_and_keep_source_amounts_separate(self):
        result = {'tool': 'sources', 'mode': '家庭版', 'evidence': {'sources': [
            {'source': '画册', 'member': '春枝', 'metric': 'income', 'amount': 500, 'recordCount': 4,
             'remarks': ['起稿；修改', '=SUM(A1)', '起稿；修改', '']},
            {'source': '画册', 'member': '云川', 'metric': 'income', 'amount': 80, 'remarks': ['起稿；修改']},
            {'source': '排版', 'member': '春枝', 'metric': 'expense', 'amount': 20, 'remarks': ['排版']},
            {'source': '无备注来源', 'member': '春枝', 'metric': 'income', 'amount': 70, 'remarks': []},
        ]}}
        artifacts = build_artifacts([result], {'metric': 'income', 'includeSources': True})
        summary, mapping = artifacts
        self.assertEqual([row['amount'] for row in summary['rows']], [500, 80, 70])
        self.assertEqual([(r['member'], r['remark']) for r in mapping['rows']],
                         [('春枝', '起稿；修改'), ('春枝', '=SUM(A1)'), ('云川', '起稿；修改')])
        self.assertEqual({col['key'] for col in mapping['columns']}, {'remark', 'member', 'source', 'direction'})

    def test_forecast_can_show_bounded_lineage_without_a_separate_catalog(self):
        forecast = forecast_result()
        components = forecast['evidence']['sourceForecasts']
        components[:] = [dict(components[0], source=f'来源{i}', remarks=[f'阶段{j}' for j in range(4)])
                         for i in range(65)]
        plain = build_artifacts([forecast], {'metric': 'income'})
        self.assertEqual(len(plain), 1)
        summary, mapping = build_artifacts([forecast], {'includeLineage': True, 'metric': 'income'})
        self.assertEqual(len(mapping['rows']), 240)
        self.assertTrue(any('20' in n and '省略' in n for n in mapping['notes']))

    def test_forecast_audit_preserves_excluded_historical_amount_and_translates_statuses(self):
        forecast = forecast_result()
        components = forecast['evidence']['sourceForecasts']
        for row, status in zip(components, ['observed', 'new', 'sparse']):
            row.update(status=status, amount=9000)
        components.extend([
            {'source': '已停用的历史来源', 'amount': 600, 'estimate': 0, 'status': 'inactive', 'excluded': False},
            {'source': '一次性入账', 'amount': 6000, 'estimate': 0, 'status': 'excluded', 'excluded': True},
        ])
        table = build_artifacts([forecast])[0]
        known_statuses = {'observed', 'new', 'sparse', 'inactive', 'excluded'}
        statuses = {row['status'] for row in table['rows'][:-1]}
        self.assertEqual(len(statuses), 5)
        self.assertTrue(statuses.isdisjoint(known_statuses))
        self.assertEqual(table['rows'][-2]['historicalAmount'], 6000)
        self.assertEqual(table['rows'][-2]['estimate'], 0)
        total = table['rows'][-1]
        self.assertEqual(total['estimate'], sum(row['estimate'] for row in table['rows'][:-1]))
        self.assertEqual((total['lower'], total['upper']), (6500, 7500))

    def test_markdown_cells_cannot_inject_rows_or_headings(self):
        text = render_artifacts_markdown([{'id': 'a1', 'kind': 'table', 'title': '来源',
            'columns': [{'key': 'source', 'label': '来源'}], 'rows': [{'source': 'A|B\n### bad'}], 'notes': []}])
        self.assertIn('A\\|B', text)
        self.assertNotIn('\n### bad', text)

    def test_markdown_cells_wrap_ledger_links_in_literal_code_spans(self):
        note = '[账户](https://example.invalid/a) ![图](https://example.invalid/b) https://example.invalid/c `内` | 末\n下一行'
        text = render_artifacts_markdown([{'id': 'a1', 'kind': 'table', 'title': '备注表',
            'columns': [{'key': 'note', 'label': '备注'}, {'key': 'amount', 'label': '金额', 'unit': '元'}],
            'rows': [{'note': note, 'amount': 42.25}]}])
        self.assertTrue(text.startswith('### 备注表\n\n| 备注 | 金额（元） |'))
        self.assertEqual(text.splitlines()[-1],
                         '| `` [账户](https://example.invalid/a) ![图](https://example.invalid/b) '
                         'https://example.invalid/c `内` \\| 末 下一行 `` | 42.25 |')

    def test_aggregate_table_preserves_same_name_groups_without_publishing_opaque_keys(self):
        result = aggregate_result([
            aggregate_order(2025, -40),
            aggregate_order(2025, -20, member_key='opaque-member-b'),
            aggregate_order(2025, -2, member_key='opaque-member-c'),
        ], groupBy=['member', 'source'], limit=2)
        for row in result['evidence']['rows']:
            row['sourceKey'] = 'opaque-source'
        artifacts = build_artifacts([result], {'table': True})
        self.assertEqual(len(artifacts), 1)
        table = artifacts[0]
        self.assertEqual(table['kind'], 'table')
        self.assertEqual([(row['member'], row['source'], row['expense']) for row in table['rows']],
                         [('同名成员', '交通', 40), ('同名成员', '交通', 20)])
        keys = {column['key'] for column in table['columns']}
        self.assertTrue(all(set(row) == keys for row in table['rows']))
        self.assertNotIn('memberKey', keys)
        self.assertNotIn('sourceKey', keys)
        self.assertNotIn('opaque-', dumps(table))
        self.assertTrue(any('1 个分组' in note for note in table['notes']))

    def test_aggregate_export_shows_period_statistics_and_keeps_unknown_comparisons(self):
        periods = [{'label': str(year), 'startDate': f'{year}-01-01', 'endDate': f'{year}-01-31'}
                   for year in (2024, 2025, 2026)]
        result = aggregate_result([
            aggregate_order(2024, -10), aggregate_order(2024, -10), aggregate_order(2024, -40),
            aggregate_order(2025, -80),
        ], metric='expense', periods=periods)
        artifacts = build_artifacts([result], {'export': True, 'table': False, 'chart': 'none'})
        self.assertEqual(len(artifacts), 2)
        table, comparison = artifacts
        self.assertEqual((table['kind'], comparison['kind']), ('table', 'table'))
        first = next(row for row in table['rows'] if row['period'] == '2024')
        self.assertEqual(
            tuple(first[key] for key in ('amount', 'count', 'meanTransactionAmount', 'medianTransactionAmount', 'sharePercent')),
            (60, 3, 20, 10, 100),
        )
        before, missing = comparison['rows']
        self.assertEqual((before['before'], before['after'], before['change'], before['changePercent']),
                         (60, 80, 20, 33.33))
        self.assertEqual(missing['afterPeriod'], '2026')
        self.assertIsNone(missing['after'])
        self.assertIsNone(missing['change'])
        self.assertIsNone(missing['changePercent'])
        self.assertTrue(any('2026-01-01' in note and '未知' in note for note in table['notes']))
        self.assertTrue(any('占比' in note and '分母' in note for note in table['notes']))

    def test_aggregate_enumeration_shows_every_bounded_group_without_output_flags(self):
        result = aggregate_result([
            aggregate_order(2025, -amount, category=f'分类{amount}') for amount in range(1, 13)
        ], metric='expense', groupBy=['category'])
        artifacts = build_artifacts([result], {'table': False, 'export': False})
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]['kind'], 'table')
        self.assertEqual(len(artifacts[0]['rows']), 12)

    def test_aggregate_comparison_columns_use_comparison_dimensions_only(self):
        periods = [{'label': str(year), 'startDate': f'{year}-01-01', 'endDate': f'{year}-01-31'}
                   for year in (2024, 2025)]
        records = [aggregate_order(2024, -30), aggregate_order(2025, -40)]
        result = aggregate_result(records,
                                  metric='expense', groupBy=['month', 'category'], periods=periods)
        main, comparison = build_artifacts([result], {'table': True})
        self.assertIn('month', {column['key'] for column in main['columns']})
        keys = {column['key'] for column in comparison['columns']}
        self.assertIn('category', keys)
        self.assertNotIn('month', keys)
        self.assertEqual(comparison['rows'][0]['category'], '交通')
        self.assertEqual(comparison['rows'][0]['change'], 10)

        result = aggregate_result(records, metric='expense', groupBy=['month'], periods=periods)
        _, comparison = build_artifacts([result], {'table': True})
        self.assertNotIn('month', {column['key'] for column in comparison['columns']})

    def test_aggregate_discloses_partial_and_unqueried_periods(self):
        result = aggregate_result([aggregate_order(2024, -20)], metric='expense', periods=[
            {'label': '部分查询', 'startDate': '2024-01-01', 'endDate': '2024-01-31'},
            {'label': '未查询', 'startDate': '2025-01-01', 'endDate': '2025-01-31'},
        ], queried_intervals=[
            {'startDate': '2024-01-01', 'endDate': '2024-01-08'},
            {'startDate': '2024-01-15', 'endDate': '2024-01-22'},
        ])
        main, _ = build_artifacts([result], {'table': True})
        partial_note, unqueried_note = main['notes'][:2]
        self.assertIn('部分已查询', partial_note)
        self.assertIn('2024-01-08', partial_note)
        self.assertIn('2024-01-15', partial_note)
        self.assertIn('找到 1 笔', partial_note)
        self.assertIn('金额未知', partial_note)
        self.assertIn('未查询', unqueried_note)
        self.assertIn('金额未知', unqueried_note)
        self.assertNotIn('缺少账单记录', partial_note + unqueried_note)

    def test_aggregate_category_bar_chart_uses_the_requested_metric(self):
        result = aggregate_result([
            aggregate_order(2025, -50), aggregate_order(2025, 500),
            aggregate_order(2025, -80, category='餐饮'),
        ], groupBy=['category'])
        artifacts = build_artifacts([result], {
            'chart': 'bar', 'chartTarget': 'aggregate', 'metric': 'expense', 'table': False,
        })
        self.assertEqual({item['kind'] for item in artifacts}, {'table', 'bar'})
        chart = next(item for item in artifacts if item['kind'] == 'bar')
        self.assertEqual(chart['xKey'], 'category')
        self.assertEqual(chart['series'], [{'key': 'expense', 'label': '支出'}])
        self.assertEqual(chart['rows'], [{'category': '交通', 'expense': 50}, {'category': '餐饮', 'expense': 80}])

    def test_aggregate_multidimensional_chart_request_retains_full_table(self):
        result = aggregate_result([aggregate_order(2025, -50)], groupBy=['member', 'category'])
        artifacts = build_artifacts([result], {
            'chart': 'bar', 'chartTarget': 'aggregate', 'metric': 'all', 'table': False,
        })
        self.assertEqual([item['kind'] for item in artifacts], ['table'])
        self.assertEqual((artifacts[0]['rows'][0]['member'], artifacts[0]['rows'][0]['category']),
                         ('同名成员', '交通'))
        self.assertTrue(any('一个分组维度' in note for note in artifacts[0]['notes']))

    def test_aggregate_period_chart_keeps_unqueried_period_as_unknown(self):
        periods = [{'label': str(year), 'startDate': f'{year}-01-01', 'endDate': f'{year}-01-31'}
                   for year in (2024, 2025, 2026)]
        result = aggregate_result([aggregate_order(2024, -20), aggregate_order(2026, -80)],
                                  metric='expense', periods=periods, queried_intervals=[periods[0], periods[2]])
        artifacts = build_artifacts([result], {'chart': 'line', 'chartTarget': 'aggregate'})
        chart = next(item for item in artifacts if item['kind'] == 'line')
        self.assertEqual(chart['rows'], [
            {'period': '2024', 'expense': 20},
            {'period': '2025', 'expense': None},
            {'period': '2026', 'expense': 80},
        ])
        self.assertTrue(any('2025-01-01' in note and '未查询' in note for note in chart['notes']))

    def test_aggregate_export_with_no_observed_group_keeps_amount_unknown(self):
        result = aggregate_result([], groupBy=['category'], metric='net', periods=[
            {'label': '缺账区间', 'startDate': '2026-01-01', 'endDate': '2026-01-31'},
        ])
        artifacts = build_artifacts([result], {'export': True})
        self.assertEqual(len(artifacts), 1)
        table = artifacts[0]
        self.assertEqual(table['rows'][0]['period'], '缺账区间')
        self.assertIsNone(table['rows'][0]['amount'])
        self.assertIsNone(table['rows'][0]['count'])
        self.assertIn('未知', render_artifacts_markdown([table]))


if __name__ == '__main__':
    unittest.main()
