import unittest

from app.agent import evidence_report
from app.agent.artifacts import build_artifacts, render_artifacts_markdown


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
            self.assertIn(remark, lineage['rows'][0]['remark'])
        self.assertEqual(lineage['rows'][0]['source'], '星港项目')
        self.assertTrue(any('4 条' in note and '160 字' in note for note in lineage['notes']))
        self.assertTrue(any('12' in note and '来源' in note for note in lineage['notes']))

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


if __name__ == '__main__':
    unittest.main()
