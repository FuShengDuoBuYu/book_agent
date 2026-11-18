import unittest

from app.agent import evidence_report


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


class ArtifactTests(unittest.TestCase):
    def test_forecast_evidence_keeps_method_and_member_source_breakdown(self):
        facts = evidence_report.build_facts([forecast_result()])
        text = '\n'.join(row['text'] for row in facts)
        self.assertIn('按来源', text)
        self.assertIn('带球', text)
        self.assertIn('成员乙', text)

    def test_forecast_table_total_reconciles_and_is_downloadable(self):
        from app.agent.artifacts import build_artifacts, render_artifacts_markdown
        artifacts = build_artifacts([forecast_result()], {'table': True, 'metric': 'income'})
        table = next(row for row in artifacts if row['kind'] == 'table')
        self.assertEqual([row['estimate'] for row in table['rows']], [1200, 5000, 900, 7100])
        self.assertIn('成员乙', render_artifacts_markdown(artifacts))

    def test_income_chart_does_not_substitute_expense_and_preserves_unknown_month(self):
        from app.agent.artifacts import build_artifacts
        result = {'tool': 'trends', 'mode': '个人版', 'evidence': {'months': [
            {'month': '2026-08', 'incomeTotal': 6000, 'expenseTotal': 200, 'netIncomeMinusExpense': 5800, 'partial': False},
            {'month': '2026-10', 'incomeTotal': 7000, 'expenseTotal': 900, 'netIncomeMinusExpense': 6100, 'partial': False},
        ], 'coverageNote': '缺失月份未知。'}}
        artifacts = build_artifacts([result], {'chart': 'line', 'metric': 'income'},
                                    window={'startDate': '2026-08-01', 'endDate': '2026-10-31'})
        chart = next(row for row in artifacts if row['kind'] == 'line')
        self.assertEqual(chart['series'], [{'key': 'income', 'label': '收入'}])
        self.assertEqual([row['income'] for row in chart['rows']], [6000, None, 7000])
        self.assertNotIn('expense', chart['rows'][0])

    def test_markdown_cells_cannot_inject_rows_or_headings(self):
        from app.agent.artifacts import render_artifacts_markdown
        text = render_artifacts_markdown([{'id': 'a1', 'kind': 'table', 'title': '来源',
            'columns': [{'key': 'source', 'label': '来源'}], 'rows': [{'source': 'A|B\n### bad'}], 'notes': []}])
        self.assertIn('A\\|B', text)
        self.assertNotIn('\n### bad', text)


if __name__ == '__main__':
    unittest.main()
