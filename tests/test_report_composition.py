import unittest
from copy import deepcopy

from app.agent.report_composition import ReportComment, ReportOutline, compose_report


def section(title, goal, *identifiers):
    return {'title': title, 'answerGoal': goal, 'factIds': list(identifiers)}


def payload_for_composition():
    return {
        'question': '按类别说明支出变化。',
        'understanding': {'requestedAnswers': ['解释哪些类别增加']},
        'analysisScope': {'metric': 'expense', 'window': '九月到十月'},
        'userConditions': {'protectedCategories': ['房租']},
        'history': '不应回放的旧助手分析。',
        'facts': [{'id': 'f1', 'text': '交通支出变化。', 'required': True, 'binding': {'measure': 'expense'}},
                  {'id': 'f2', 'text': '餐饮支出变化。', 'required': False},
                  {'id': 'f3', 'text': '与本段均无关的其他事实。', 'required': False}],
        'values': {'v1': 'f1 变化额 = 100 元', 'v7': 'f2 变化额 = 80 元', 'v10': 'f3 参考项 = 200 元'},
        'artifacts': [{'id': 'a1', 'title': '分类统计', 'rows': [{'label': '其他资料'}], 'totalRows': 20}],
    }


class FakeComposition:
    def __init__(self, *sections):
        self.outline = {'sections': list(sections)}
        self.calls = []

    async def invoke(self, schema, system, payload):
        self.calls.append((schema, deepcopy(payload)))
        if schema is ReportOutline:
            return self.outline
        if schema is ReportComment:
            return {'analysis': '根据该段账单证据解释类别变化。'}
        raise AssertionError(f'Unexpected schema: {schema}')


class ReportCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_comment_receives_only_its_bound_facts_and_original_value_ids(self):
        payload = payload_for_composition()
        initial = deepcopy(payload)
        fake = FakeComposition(section('交通分析', '解释交通支出变化', 'f1'),
                               section('餐饮分析', '解释餐饮支出变化', 'f2'))
        draft = await compose_report(payload, fake.invoke)
        self.assertEqual([schema for schema, _ in fake.calls], [ReportOutline, ReportComment, ReportComment])
        outline = fake.calls[0][1]
        self.assertEqual(outline['facts'], payload['facts'])
        self.assertEqual(outline['understanding'], payload['understanding'])
        self.assertEqual(outline['artifacts'], payload['artifacts'])
        self.assertNotIn('values', outline)
        for index, (key, value) in enumerate([('v1', 'f1'), ('v7', 'f2')], 1):
            comment = fake.calls[index][1]
            self.assertEqual(comment['section'], fake.outline['sections'][index - 1])
            self.assertEqual(comment['facts'], [next(row for row in payload['facts'] if row['id'] == value)])
            self.assertEqual(comment['values'], {key: payload['values'][key]})
            self.assertEqual(comment['question'], payload['question'])
            self.assertEqual(comment['analysisScope'], payload['analysisScope'])
            self.assertEqual(comment['userConditions'], payload['userConditions'])
            self.assertNotIn('artifacts', comment)
            self.assertNotIn('history', comment)
        self.assertEqual([(row.title, row.factIds) for row in draft.sections],
                         [('交通分析', ['f1']), ('餐饮分析', ['f2'])])
        self.assertEqual(payload, initial)

    async def test_unknown_outline_reference_is_rejected_before_any_comment(self):
        fake = FakeComposition(section('交通分析', '解释交通', 'f1'),
                               section('无证据', '猜测其他范围', 'fabricated'))
        with self.assertRaisesRegex(ValueError, '证据'):
            await compose_report(payload_for_composition(), fake.invoke)
        self.assertEqual([schema for schema, _ in fake.calls], [ReportOutline])

    async def test_omitted_required_evidence_has_independent_blank_analysis_sections(self):
        fake = FakeComposition(section('交通分析', '解释交通', 'f1'))
        payload = payload_for_composition()
        payload['facts'] = [{'id': f'f{n}', 'text': '原始必需证据。', 'required': True}
                            for n in range(1, 12)]
        draft = await compose_report(payload, fake.invoke)
        model, first, second = draft.sections
        self.assertEqual(model.factIds, ['f1'])
        self.assertTrue(model.analysis)
        self.assertEqual((first.analysis, second.analysis), ('', ''))
        self.assertEqual(first.factIds, [f'f{n}' for n in range(2, 10)])
        self.assertEqual(second.factIds, ['f10', 'f11'])
        self.assertEqual([schema for schema, _ in fake.calls], [ReportOutline, ReportComment])

    async def test_too_many_required_evidence_sections_raise_before_comment(self):
        fake = FakeComposition(section('交通分析', '解释交通', 'f1'))
        payload = payload_for_composition()
        payload['facts'] = [{'id': f'f{n}', 'text': '必需证据。', 'required': True}
                            for n in range(1, 43)]
        with self.assertRaisesRegex(ValueError, '必需证据'):
            await compose_report(payload, fake.invoke)
        self.assertEqual([schema for schema, _ in fake.calls], [ReportOutline])

    async def test_diagnostics_follow_the_bound_section_when_outline_reorders_sections(self):
        payload = payload_for_composition()
        payload['previousReport'] = {'sections': [
            {'title': '交通分析', 'factIds': ['f1'], 'analysis': '旧段落错误结论'},
            {'title': '餐饮分析', 'factIds': ['f2'], 'analysis': '另一个旧段落错误结论'},
        ]}
        first = '第1段：不要把支出变化当成余额。'
        second = '第2段：不要把账单笔数当成购买数量。'
        common = '解释时保留用户明确保护的房租。'
        payload['corrections'] = [first, second, common]
        fake = FakeComposition(section('餐饮分析', '解释餐饮', 'f2'),
                               section('交通分析', '解释交通', 'f1'))
        await compose_report(payload, fake.invoke)
        outline, dining, travel = [entry[1] for entry in fake.calls]
        self.assertEqual(outline['corrections'], [first, second, common])
        self.assertEqual(dining['corrections'], [second, common])
        self.assertEqual(travel['corrections'], [first, common])
        self.assertNotIn('旧段落错误结论', repr(fake.calls))
        self.assertNotIn('previousReport', dining)
        self.assertNotIn('previousReport', travel)

    async def test_comment_budget_never_drops_bound_value_to_force_a_call(self):
        payload = payload_for_composition()
        payload['values']['v1'] = 'f1 ' + '必须保留的已绑定数量依据。' * 700
        fake = FakeComposition(section('交通分析', '解释交通', 'f1'))
        with self.assertRaisesRegex(ValueError, '预算'):
            await compose_report(payload, fake.invoke)
        self.assertEqual([schema for schema, _ in fake.calls], [ReportOutline])


if __name__ == '__main__':
    unittest.main()
