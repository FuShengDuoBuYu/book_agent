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


WINDOW = {'startDate': '2026-08-01', 'endDate': '2026-10-31'}


def bound_fact(identifier, text, group, tool, *, headline=False, primary=True, scope='个人版', window=WINDOW, **extra):
    binding = {'resultGroup': group, 'tool': tool, 'scope': scope, 'historyWindow': window,
               'metric': 'income', 'primary': primary, **extra}
    if headline:
        binding['headline'] = True
    return {'id': identifier, 'text': text, 'required': headline and primary, 'binding': binding}


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
    async def test_native_outline_cannot_generate_artifact_ids_or_new_goals(self):
        from app.agent.report_composition import outline_schema
        p = payload_for_composition()
        p['answerGoals'] = p['understanding']['requestedAnswers']
        schema = outline_schema(p)['$defs']['OutlineSection']['properties']
        self.assertEqual(schema['factIds']['items']['enum'], ['f1', 'f2', 'f3'])
        self.assertEqual(schema['answerGoal']['enum'], ['解释哪些类别增加'])
        self.assertNotIn('抗风险弱', schema['title']['enum'])

    async def test_outline_cannot_invent_a_new_answer_goal_from_its_observations(self):
        fake = FakeComposition(section('分析结论', '证明收入单一所以抗风险能力弱', 'f1'))
        with self.assertRaisesRegex(ValueError, '目标'):
            await compose_report(payload_for_composition(), fake.invoke)
        self.assertEqual([schema for schema, _ in fake.calls], [ReportOutline])

    async def test_each_comment_receives_only_its_bound_facts_and_original_value_ids(self):
        payload = payload_for_composition()
        initial = deepcopy(payload)
        fake = FakeComposition(section('变化来源', '解释哪些类别增加', 'f1'),
                               section('统计口径', '解释哪些类别增加', 'f2'))
        draft = await compose_report(payload, fake.invoke)
        self.assertEqual([schema for schema, _ in fake.calls], [ReportOutline, ReportComment, ReportComment])
        outline = fake.calls[0][1]
        self.assertEqual(outline['facts'], payload['facts'])
        self.assertEqual(outline['understanding'], payload['understanding'])
        self.assertEqual(outline['artifacts'], payload['artifacts'])
        self.assertNotIn('values', outline)
        for index, (key, value) in enumerate([('v1', 'f1'), ('v7', 'f2')], 1):
            comment = fake.calls[index][1]
            self.assertEqual(comment['section'], {k: v for k, v in fake.outline['sections'][index - 1].items() if k != 'factIds'})
            self.assertEqual(comment['computedFacts'], [{k: v for k, v in next(row for row in payload['facts'] if row['id'] == value).items() if k in {'text', 'binding'}}])
            self.assertEqual(comment['values'], {key: payload['values'][key].split(' ', 1)[1]})
            self.assertEqual(comment['question'], payload['question'])
            self.assertEqual(comment['analysisScope'], payload['analysisScope'])
            self.assertEqual(comment['userConditions'], payload['userConditions'])
            self.assertNotIn('artifacts', comment)
            self.assertNotIn('history', comment)
        self.assertEqual([(row.title, row.factIds) for row in draft.sections],
                         [('变化来源', ['f1']), ('统计口径', ['f2'])])
        self.assertEqual(payload, initial)

    async def test_unknown_outline_reference_is_rejected_before_any_comment(self):
        fake = FakeComposition(section('变化来源', '解释哪些类别增加', 'f1'),
                               section('需要确认的事项', '猜测其他范围', 'fabricated'))
        with self.assertRaisesRegex(ValueError, '证据'):
            await compose_report(payload_for_composition(), fake.invoke)
        self.assertEqual([schema for schema, _ in fake.calls], [ReportOutline])

    async def test_omitted_required_evidence_has_independent_blank_analysis_sections(self):
        fake = FakeComposition(section('变化来源', '解释哪些类别增加', 'f1'))
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
        fake = FakeComposition(section('变化来源', '解释哪些类别增加', 'f1'))
        payload = payload_for_composition()
        payload['facts'] = [{'id': f'f{n}', 'text': '必需证据。', 'required': True}
                            for n in range(1, 43)]
        with self.assertRaisesRegex(ValueError, '必需证据'):
            await compose_report(payload, fake.invoke)
        self.assertEqual([schema for schema, _ in fake.calls], [ReportOutline])

    async def test_diagnostics_follow_the_bound_section_when_outline_reorders_sections(self):
        payload = payload_for_composition()
        payload['previousReport'] = {'sections': [
            {'title': '变化来源', 'factIds': ['f1'], 'analysis': '旧段落错误结论'},
            {'title': '统计口径', 'factIds': ['f2'], 'analysis': '另一个旧段落错误结论'},
        ]}
        first = '第1段：不要把支出变化当成余额。'
        second = '第2段：不要把账单笔数当成购买数量。'
        common = '解释时保留用户明确保护的房租。'
        payload['corrections'] = [first, second, common]
        fake = FakeComposition(section('统计口径', '解释哪些类别增加', 'f2'),
                               section('变化来源', '解释哪些类别增加', 'f1'))
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
        fake = FakeComposition(section('变化来源', '解释哪些类别增加', 'f1'))
        with self.assertRaisesRegex(ValueError, '预算'):
            await compose_report(payload, fake.invoke)
        self.assertEqual([schema for schema, _ in fake.calls], [ReportOutline])

    async def test_forecast_parts_share_the_computed_headline_and_may_merge(self):
        payload = payload_for_composition()
        goals = ['解释收入构成', '核对预测合计', '说明数据来源']
        payload['understanding']['requestedAnswers'] = goals
        payload['facts'] = [
            bound_fact('f1', '条件预测为 3,700 元。', 'g1', 'forecast', headline=True),
            bound_fact('f5', '各来源按参考月份分别计算。', 'g1', 'forecast', headline=True),
            bound_fact('f2', '历史样本共 19,000 元。', 'g1', 'forecast', source='项目收入'),
            bound_fact('f3', '同范围历史来源汇总。', 'g2', 'sources', source='项目明细', primary=False),
            bound_fact('f4', '另一个账本的总额是 83,700 元。', 'g3', 'aggregate', scope='家庭版'),
        ]
        payload['values'] = {'v1': 'f1 条件预测 = 3,700 元', 'v2': 'f2 历史样本 = 19,000 元',
                             'v12': 'f4 总额 = 83,700 元'}
        fake = FakeComposition(section('收入构成', goals[0], 'f2'), section('分析结论', goals[1], 'f1'),
                               section('统计口径', goals[2], 'f3'))
        draft = await compose_report(payload, fake.invoke)
        self.assertEqual([schema for schema, _ in fake.calls], [ReportOutline, ReportComment])
        self.assertEqual(set(draft.sections[0].factIds), {'f1', 'f2', 'f3', 'f5'})
        comment = fake.calls[-1][1]
        self.assertEqual(comment['section']['answerGoals'], goals)
        self.assertEqual({row['text'] for row in comment['computedFacts']},
                         {row['text'] for row in payload['facts'][:4]})
        self.assertEqual(comment['values'], {'v1': '条件预测 = 3,700 元', 'v2': '历史样本 = 19,000 元'})
        self.assertNotIn('83,700', repr(comment))
        inventory = comment['availableEvidence']
        self.assertTrue(any(row['tool'] == 'forecast' and row.get('sourceBreakdown') for row in inventory))
        self.assertTrue(any(row['tool'] == 'aggregate' and row['scope'] == '家庭版' for row in inventory))
        self.assertNotIn('项目收入', repr(inventory))

    async def test_history_with_different_scope_window_or_missing_window_does_not_inherit_headline(self):
        payload = payload_for_composition()
        goal = payload['understanding']['requestedAnswers'][0]
        payload['facts'] = [
            bound_fact('f1', '预测合计为 3,700 元。', 'g1', 'forecast', headline=True),
            bound_fact('f2', '另一期间的个人账本来源。', 'g2', 'sources',
                       window={'startDate': '2026-07-01', 'endDate': '2026-07-31'}),
            bound_fact('f3', '同一期间的家庭账本来源。', 'g3', 'sources', scope='家庭版'),
            bound_fact('f4', '范围未确认的来源。', 'g4', 'sources', window=None),
        ]
        fake = FakeComposition(section('变化来源', goal, 'f2'), section('收入构成', goal, 'f3'),
                               section('统计口径', goal, 'f4'))
        draft = await compose_report(payload, fake.invoke)
        self.assertEqual([row.factIds for row in draft.sections], [['f2'], ['f3'], ['f4'], ['f1']])
        self.assertEqual(draft.sections[-1].analysis, '')
        for _, comment in fake.calls[1:]:
            self.assertNotIn('3,700', repr(comment))

    async def test_same_window_alternative_forecasts_are_not_borrowed_by_history(self):
        payload = payload_for_composition()
        goal = payload['understanding']['requestedAnswers'][0]
        payload['facts'] = [
            bound_fact('f1', '第一种预测。', 'g1', 'forecast', headline=True),
            bound_fact('f2', '第二种预测。', 'g2', 'forecast', headline=True),
            bound_fact('f3', '历史来源。', 'g3', 'sources', primary=False),
        ]
        fake = FakeComposition(section('统计口径', goal, 'f3'))
        draft = await compose_report(payload, fake.invoke)
        self.assertEqual([row.factIds for row in draft.sections], [['f3'], ['f1', 'f2']])
        self.assertEqual(draft.sections[-1].analysis, '')

    async def test_different_filters_do_not_inherit_headline_in_the_same_book_and_window(self):
        payload = payload_for_composition()
        goal = payload['understanding']['requestedAnswers'][0]
        payload['facts'] = [
            bound_fact('f1', '目标分类的条件预测。', 'g1', 'forecast', headline=True,
                       filters=[{'category': '工资'}]),
            bound_fact('f2', '另一分类的历史来源。', 'g2', 'sources', filters=[{'category': '稿费'}]),
        ]
        fake = FakeComposition(section('统计口径', goal, 'f2'))
        draft = await compose_report(payload, fake.invoke)
        self.assertEqual([row.factIds for row in draft.sections], [['f2'], ['f1']])
        self.assertEqual(draft.sections[-1].analysis, '')

    async def test_headline_dependency_over_capacity_raises_without_discarding_selected_facts(self):
        payload = payload_for_composition()
        goal = payload['understanding']['requestedAnswers'][0]
        payload['facts'] = [bound_fact('f1', '预测合计。', 'g1', 'forecast', headline=True)] + [
            bound_fact(f'f{i}', '来源预测贡献。', 'g1', 'forecast') for i in range(2, 10)
        ]
        fake = FakeComposition(section('收入构成', goal, *(f'f{i}' for i in range(2, 10))))
        with self.assertRaisesRegex(ValueError, '主结论.*8'):
            await compose_report(payload, fake.invoke)
        self.assertEqual([schema for schema, _ in fake.calls], [ReportOutline])

    async def test_duplicate_evidence_sections_keep_only_existing_answer_goals(self):
        payload = payload_for_composition()
        goals = ['解释哪些类别增加', '核对统计口径']
        payload['understanding']['requestedAnswers'] = goals
        fake = FakeComposition(section('变化来源', goals[0], 'f1'), section('统计口径', goals[1], 'f1'))
        draft = await compose_report(payload, fake.invoke)
        self.assertEqual([row.factIds for row in draft.sections], [['f1']])
        self.assertEqual(fake.calls[1][1]['section']['answerGoals'], goals)


if __name__ == '__main__':
    unittest.main()
