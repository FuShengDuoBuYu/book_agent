import unittest
from copy import deepcopy

from app.agent.report_context import build_report_context, repair_context
from app.agent.report_values import value_catalog
from app.schemas.report import ReportDraft


SYSTEM = '只使用本轮账本证据作答。'
SCHEMA = {'type': 'object', 'properties': {'sections': {'type': 'array'}}}


def fact(number, *, required, text, label='统计金额', value=1234):
    return {'id': f'f{number}', 'text': text, 'required': required,
            'values': [{'label': label, 'value': value, 'unit': '元'}],
            'calendars': ['2026-09', '2026-10']}


def base_payload():
    return {
        'question': '按分类列出支出，并说明哪类变化最大。只看个人账本。',
        'understanding': {'requestedAnswers': ['展示分类明细', '追问哪一类变化最大']},
        'userConditions': {'excludedCategories': ['医疗'], 'targetMonth': '2026-11'},
        'instruction': '沿用上轮的基准月份，给出变化金额和统计口径。',
        'history': '',
        'artifacts': [],
    }


class ReportContextTests(unittest.TestCase):
    def test_model_projection_uses_a_single_compact_display_per_value(self):
        original = [fact(1, required=True, text='进账 1,234 元。', label='进账')]
        payload, selected, stats = build_report_context(base_payload(), original, SYSTEM, ReportDraft)
        self.assertEqual(payload['facts'], [{'id': 'f1', 'text': '进账 1,234 元。', 'required': True}])
        self.assertEqual(payload['values'], {'v1': 'f1 进账 = 1,234 元'})
        self.assertEqual(selected, original)
        self.assertEqual(selected[0]['calendars'], ['2026-09', '2026-10'])
        self.assertEqual(stats['promptBudgetTokens'], 8192 - 2048 - 256)

    def test_compact_source_text_keeps_eight_sources_without_changing_internal_evidence(self):
        base = base_payload()
        base['question'] = '请说明八个收入来源及汇总口径。'
        base['understanding'] = {'requestedAnswers': ['逐项解释八个收入来源', '复核汇总口径']}
        required = fact(1, required=True, text='收入预测以完整历史月份为基准。')
        sources = [
            {**fact(number + 1, required=False,
                    text=f'收入来源{number}。备注依据：' + '该来源的详细账单备注。' * 55,
                    label='来源预测贡献', value=number * 100),
             'modelText': f'来源{number}的预测贡献为 {number * 100} 元。'}
            for number in range(1, 9)
        ]
        original = [required, *sources]
        payload, selected, stats = build_report_context(base, original, SYSTEM, ReportDraft)

        self.assertEqual(payload['question'], base['question'])
        self.assertEqual(payload['understanding'], base['understanding'])
        self.assertEqual(payload['facts'][0], {'id': 'f1', 'required': True, 'text': required['text']})
        self.assertEqual([item['text'] for item in payload['facts'][1:]],
                         [item['modelText'] for item in sources])
        self.assertEqual(selected, original)
        self.assertEqual(payload['values']['v9'], 'f9 来源预测贡献 = 800 元')
        self.assertEqual(stats['omittedOptionalFactCount'], 0)
        self.assertLessEqual(stats['estimatedPromptTokens'], stats['promptBudgetTokens'])

    def test_pressure_keeps_question_instructions_required_facts_and_artifact_columns(self):
        base = base_payload()
        base['history'] = '较早的聊天记录' * 450 + '\n最新一句是此前问的餐饮支出。'
        columns = [{'key': f'col{i}', 'label': f'第{i}列', 'unit': '元'} for i in range(1, 13)]
        base['artifacts'] = [{
            'id': 'a1', 'kind': 'table', 'title': '分类支出', 'columns': columns, 'notes': ['同一账本口径'],
            'rows': [{column['key']: '长账单名称' * 25 for column in columns} for _ in range(18)],
            'totalRows': 93,
        }]
        facts = [fact(1, required=True, text='分类变化问的是环比。'),
                 fact(2, required=False, text='次要备注。' * 350),
                 fact(3, required=True, text='基准月份为 2026-10。'),
                 fact(4, required=False, text='历史备注。' * 180)]
        before = deepcopy(base)
        payload, selected, stats = build_report_context(
            base, facts, SYSTEM, SCHEMA, num_ctx=1800, num_predict=384,
        )
        for key in ('question', 'understanding', 'userConditions', 'instruction'):
            self.assertEqual(payload[key], base[key])
        self.assertEqual([row['id'] for row in selected], ['f1', 'f3'])
        self.assertEqual(payload['artifacts'][0]['columns'][-1], columns[-1])
        self.assertEqual(payload['artifacts'][0]['notes'], ['同一账本口径'])
        self.assertEqual(payload['artifacts'][0]['totalRows'], 93)
        self.assertLess(len(payload['artifacts'][0]['rows']), 18)
        self.assertTrue(base['history'].endswith(payload['history']))
        self.assertLessEqual(stats['estimatedPromptTokens'], stats['promptBudgetTokens'])
        self.assertEqual(stats['omittedOptionalFactCount'], 2)
        self.assertEqual(base, before)

    def test_history_keeps_latest_suffix_when_compacted(self):
        base = base_payload()
        base['history'] = '许多旧话题。' * 600 + '\n最后一轮确认只看餐饮。'
        payload, _, stats = build_report_context(
            base, [fact(1, required=True, text='使用本轮事实。')], SYSTEM, SCHEMA,
            num_ctx=1550, num_predict=384,
        )
        self.assertLess(len(payload['history']), len(base['history']))
        self.assertTrue(payload['history'].endswith('最后一轮确认只看餐饮。'))
        self.assertTrue(base['history'].endswith(payload['history']))
        self.assertLessEqual(stats['estimatedPromptTokens'], stats['promptBudgetTokens'])

    def test_repair_excerpts_fit_without_changing_fact_ids_or_values(self):
        selected_input = [fact(1, required=True, text='交通类变化最大。', value=88),
                          fact(2, required=False, text='餐饮类第二。', value=72)]
        budget = {'num_ctx': 1950, 'num_predict': 384}
        payload, selected, _ = build_report_context(base_payload(), selected_input, SYSTEM, SCHEMA, **budget)
        original = deepcopy(payload)
        prior = {'sections': [
            {'title': f'错误摘要{i}', 'factIds': ['f1', 'f2'], 'analysis': '待缩短的分析文字。' * 160}
            for i in range(1, 7)
        ]}
        issues = ['先纠正交通变化的方向。'] + ['其余冗长问题。' * 220] * 8
        repair, stats = repair_context(payload, prior, issues, SYSTEM, SCHEMA, **budget)
        self.assertEqual(repair['facts'], payload['facts'])
        self.assertEqual(repair['values'], {
            row['id']: f"{row['factId']} {row['label']} = {row['display']}" for row in value_catalog(selected)
        })
        self.assertEqual(repair['question'], payload['question'])
        self.assertEqual(repair['corrections'][0], issues[0])
        self.assertEqual(repair['previousReport']['sections'][0]['title'], '错误摘要1')
        self.assertEqual(repair['previousReport']['sections'][0]['factIds'], ['f1', 'f2'])
        self.assertLess(len(repair['previousReport']['sections'][0]['analysis']), len(prior['sections'][0]['analysis']))
        self.assertLessEqual(stats['estimatedPromptTokens'], stats['promptBudgetTokens'])
        self.assertEqual(payload, original)

    def test_required_facts_or_critical_question_over_budget_raise(self):
        budget = {'num_ctx': 1200, 'num_predict': 384}
        base = base_payload()
        required = [fact(1, required=True, text='必需证据。' * 850)]
        with self.assertRaisesRegex(ValueError, '预算'):
            build_report_context(base, required, SYSTEM, SCHEMA, **budget)
        base['question'] = '不能删除的问题要求。' * 850
        with self.assertRaisesRegex(ValueError, '预算'):
            build_report_context(base, [], SYSTEM, SCHEMA, **budget)

    def test_system_and_schema_consume_the_prompt_budget(self):
        budget = {'num_ctx': 1150, 'num_predict': 384}
        with self.assertRaisesRegex(ValueError, '预算'):
            build_report_context(base_payload(), [], '系统要求。' * 200, SCHEMA, **budget)
        schema = {**SCHEMA, 'description': '结构说明。' * 200}
        with self.assertRaisesRegex(ValueError, '预算'):
            build_report_context(base_payload(), [], SYSTEM, schema, **budget)

    def test_text_retry_context_preserves_prefix_and_budget(self):
        budget = {'num_ctx': 1250, 'num_predict': 384}
        initial, _, _ = build_report_context(base_payload(), [], SYSTEM, SCHEMA, **budget)
        text = '重试需要保留当前结论。' + '较早详细解释。' * 350
        repair, stats = repair_context(initial, text, [], SYSTEM, SCHEMA, **budget)
        self.assertTrue(repair['previousReport'].startswith('重试需要保留当前结论。'))
        self.assertLess(len(repair['previousReport']), len(text))
        self.assertLessEqual(stats['estimatedPromptTokens'], stats['promptBudgetTokens'])


if __name__ == '__main__':
    unittest.main()
