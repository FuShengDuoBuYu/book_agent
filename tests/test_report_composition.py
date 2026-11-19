import unittest
from copy import deepcopy

from app.agent.report_composition import ReportOutline, compose_report, grounded_analysis, outline_schema


WINDOW = {'startDate': '2026-08-01', 'endDate': '2026-10-31'}


def section(title, goal, *identifiers):
    return {'title': title, 'answerGoal': goal, 'factIds': list(identifiers)}


def bound_fact(identifier, text, group, tool, *, headline=False, primary=True, scope='个人版', window=WINDOW, **extra):
    binding = {'resultGroup': group, 'tool': tool, 'scope': scope, 'historyWindow': window,
               'metric': 'income', 'primary': primary, **extra}
    if headline:
        binding['headline'] = True
    return {'id': identifier, 'text': text, 'required': headline and primary, 'binding': binding}


def payload_for_composition():
    return {
        'question': '按类别说明支出变化。',
        'understanding': {'requestedAnswers': ['解释哪些类别增加']},
        'analysisScope': {'metric': 'expense', 'window': '九月到十月'},
        'userConditions': {'protectedCategories': ['房租']},
        'history': '不应回放的旧助手分析。',
        'facts': [
            {**bound_fact('f1', '交通支出变化。', 'g1', 'changes'), 'required': True},
            bound_fact('f2', '餐饮支出变化。', 'g2', 'changes'),
            bound_fact('f3', '其他账本的已记支出。', 'g3', 'changes', scope='家庭版'),
        ],
        'values': {'v1': 'f1 变化额 = 100 元'},
        'artifacts': [{'id': 'a1', 'title': '分类统计', 'rows': [{'label': '其他资料'}], 'totalRows': 20}],
    }


async def compose_fixture(payload, invoke, **kwargs):
    data = deepcopy(payload)
    if 'analysisFindings' not in data:
        data['analysisFindings'] = {}
        for index, fact in enumerate(data['facts'], 1):
            binding = fact.setdefault('binding', {})
            group = binding.setdefault('resultGroup', f'fixture{index}')
            binding.setdefault('tool', 'changes')
            binding.setdefault('scope', '个人版')
            data['analysisFindings'].setdefault(group, {
                'tool': binding['tool'], 'scope': binding['scope'],
                'observations': ['本组账单已核对。'],
            })
    return await compose_report(data, invoke, **kwargs)


class FakeComposition:
    def __init__(self, *sections):
        self.outline = {'sections': list(sections)}
        self.calls = []

    async def invoke(self, schema, system, payload):
        self.calls.append((schema, deepcopy(payload)))
        if schema is ReportOutline:
            return self.outline
        raise AssertionError(f'Unexpected schema: {schema}')


class ReportCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_outline_only_renders_code_findings_and_keeps_literal_variable_like_names(self):
        payload = payload_for_composition()
        payload['analysisFindings'] = {
            'g1': {'tool': 'changes', 'scope': '个人版',
                   'observations': ['“翻译{{v1}}”的已记支出上升。'],
                   'limitations': ['单笔记录无法确认日后是否持续。'],
                   'actions': ['核对该条原始账单。']},
            'g3': {'tool': 'changes', 'scope': '家庭版', 'observations': ['无关事实。']},
        }
        initial = deepcopy(payload)
        fake = FakeComposition(section('变化来源', '解释哪些类别增加', 'f1'))
        draft = await compose_report(payload, fake.invoke)
        self.assertEqual([schema for schema, _ in fake.calls], [ReportOutline])
        exactly = '【个人版】“翻译{{v1}}”的已记支出上升。 单笔记录无法确认日后是否持续。 核对该条原始账单。'
        self.assertEqual(draft.sections[0].analysis, exactly)
        outline = fake.calls[0][1]
        for key in ('question', 'understanding', 'analysisScope', 'userConditions', 'facts', 'artifacts'):
            self.assertEqual(outline[key], payload[key])
        for key in ('analysisFindings', 'values', 'quantityReferences', 'history'):
            self.assertNotIn(key, outline)
        self.assertEqual(payload, initial)

    async def test_missing_findings_raise_instead_of_substituting_another_group(self):
        fake = FakeComposition(section('变化来源', '解释哪些类别增加', 'f1'))
        with self.assertRaisesRegex(ValueError, '分析发现'):
            await compose_report(payload_for_composition(), fake.invoke)
        self.assertEqual([schema for schema, _ in fake.calls], [ReportOutline])

    async def test_native_outline_uses_available_facts_goals_and_dependency_capacity(self):
        payload = payload_for_composition()
        payload['answerGoals'] = payload['understanding']['requestedAnswers']
        payload['facts'] = [bound_fact('f1', '已算出主结论。', 'g1', 'forecast', headline=True),
                            bound_fact('f2', '已确认方法。', 'g1', 'forecast', headline=True),
                            bound_fact('f3', '来源明细。', 'g1', 'forecast')]
        schema = outline_schema(payload)['$defs']['OutlineSection']['properties']
        self.assertEqual(schema['factIds']['maxItems'], 6)
        self.assertEqual(schema['factIds']['items']['enum'], ['f1', 'f2', 'f3'])
        self.assertEqual(schema['answerGoal']['enum'], ['解释哪些类别增加'])
        self.assertNotIn('抗风险弱', schema['title']['enum'])

    async def test_outline_cannot_invent_a_new_answer_goal(self):
        fake = FakeComposition(section('分析结论', '证明收入单一所以抗风险能力弱', 'f1'))
        with self.assertRaisesRegex(ValueError, '目标'):
            await compose_fixture(payload_for_composition(), fake.invoke)

    async def test_outline_cannot_supply_free_analysis(self):
        answer = {**section('变化来源', '解释哪些类别增加', 'f1'), 'analysis': '文案不能进入最终报告。'}
        fake = FakeComposition(answer)
        with self.assertRaisesRegex(ValueError, 'analysis'):
            await compose_fixture(payload_for_composition(), fake.invoke)

    async def test_unknown_outline_reference_is_rejected(self):
        fake = FakeComposition(section('变化来源', '解释哪些类别增加', 'f1'),
                               section('需要确认的事项', '解释哪些类别增加', 'fabricated'))
        with self.assertRaisesRegex(ValueError, '证据'):
            await compose_fixture(payload_for_composition(), fake.invoke)

    async def test_omitted_required_evidence_gets_its_own_grounded_analysis(self):
        fake = FakeComposition(section('变化来源', '解释哪些类别增加', 'f1'))
        payload = payload_for_composition()
        payload['facts'] = [{'id': f'f{n}', 'text': '原始必需证据。', 'required': True}
                            for n in range(1, 12)]
        draft = await compose_fixture(payload, fake.invoke)
        model, first, second = draft.sections
        self.assertEqual(model.factIds, ['f1'])
        self.assertEqual(first.factIds, [f'f{n}' for n in range(2, 10)])
        self.assertEqual(second.factIds, ['f10', 'f11'])
        self.assertTrue(all('本组账单已核对。' in item.analysis for item in draft.sections))
        self.assertEqual([schema for schema, _ in fake.calls], [ReportOutline])

    async def test_too_many_required_evidence_sections_raise(self):
        fake = FakeComposition(section('变化来源', '解释哪些类别增加', 'f1'))
        payload = payload_for_composition()
        payload['facts'] = [{'id': f'f{n}', 'text': '必需证据。', 'required': True}
                            for n in range(1, 43)]
        with self.assertRaisesRegex(ValueError, '必需证据'):
            await compose_fixture(payload, fake.invoke)

    async def test_outline_retry_sees_corrections_without_any_old_analysis(self):
        payload = payload_for_composition()
        payload['previousReport'] = {'sections': [
            {'title': '变化来源', 'factIds': ['f1'], 'analysis': '旧段落错误结论'},
        ]}
        payload['corrections'] = ['第1段把支出变化当成余额。', '保留用户明确保护的房租。']
        fake = FakeComposition(section('变化来源', '解释哪些类别增加', 'f1'))
        draft = await compose_fixture(payload, fake.invoke)
        self.assertEqual(fake.calls[0][1]['corrections'], payload['corrections'])
        self.assertNotIn('旧段落错误结论', repr(fake.calls))
        self.assertNotIn('支出变化当成余额', draft.sections[0].analysis)

    async def test_long_code_finding_is_rendered_complete_under_small_outline_budget(self):
        payload = payload_for_composition()
        text = '必须保留的已绑定分析依据。' * 85
        payload['analysisFindings'] = {'g1': {'tool': 'changes', 'scope': '个人版', 'observations': [text]}}
        fake = FakeComposition(section('变化来源', '解释哪些类别增加', 'f1'))
        draft = await compose_report(payload, fake.invoke, num_ctx=2500)
        self.assertEqual(draft.sections[0].analysis, '【个人版】' + text)

    async def test_forecast_parts_share_the_computed_headline_and_merge(self):
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
        payload['analysisFindings'] = {
            'g1': {'tool': 'forecast', 'scope': '个人版', 'observations': ['预测沿用已记录的持续性来源。']},
            'g2': {'tool': 'sources', 'scope': '个人版', 'observations': ['查看历史来源需要区分成员。']},
            'g3': {'tool': 'aggregate', 'scope': '家庭版', 'observations': ['另一账本的解释。']},
        }
        fake = FakeComposition(section('收入构成', goals[0], 'f2'), section('分析结论', goals[1], 'f1'),
                               section('统计口径', goals[2], 'f3'))
        draft = await compose_report(payload, fake.invoke)
        self.assertEqual(len(draft.sections), 1)
        self.assertEqual(set(draft.sections[0].factIds), {'f1', 'f2', 'f3', 'f5'})
        self.assertIn('持续性来源', draft.sections[0].analysis)
        self.assertIn('区分成员', draft.sections[0].analysis)
        self.assertNotIn('另一账本', draft.sections[0].analysis)

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
        draft = await compose_fixture(payload, fake.invoke)
        self.assertEqual([row.factIds for row in draft.sections], [['f2'], ['f3'], ['f4'], ['f1']])
        self.assertTrue(draft.sections[1].analysis.startswith('【家庭版】'))
        self.assertTrue(draft.sections[-1].analysis.startswith('【个人版】'))

    async def test_identical_explicit_query_bindings_share_the_primary_decision(self):
        payload = payload_for_composition()
        payload['facts'] = [
            bound_fact('f1', '每月改善目标已经确认。', 'g1', 'optimization', headline=True,
                       window=None, queryIds=['ledger'], filters=[{'category': '不限', 'remark': ''}]),
            bound_fact('f2', '保护项已排除。', 'g1', 'optimization', headline=True,
                       window=None, queryIds=['ledger'], filters=[{'category': '不限', 'remark': ''}]),
            bound_fact('f3', '该账本的历史收支结构。', 'g2', 'cashflow',
                       window=None, queryIds=['ledger'], filters=[{'category': '不限', 'remark': ''}]),
        ]
        fake = FakeComposition(section('变化来源', '解释哪些类别增加', 'f3'))
        draft = await compose_fixture(payload, fake.invoke)
        self.assertEqual(set(draft.sections[0].factIds), {'f1', 'f2', 'f3'})

    async def test_same_window_alternative_forecasts_are_not_borrowed_by_history(self):
        payload = payload_for_composition()
        goal = payload['understanding']['requestedAnswers'][0]
        payload['facts'] = [
            bound_fact('f1', '第一种预测。', 'g1', 'forecast', headline=True),
            bound_fact('f2', '第二种预测。', 'g2', 'forecast', headline=True),
            bound_fact('f3', '历史来源。', 'g3', 'sources', primary=False),
        ]
        fake = FakeComposition(section('统计口径', goal, 'f3'))
        draft = await compose_fixture(payload, fake.invoke)
        self.assertEqual([row.factIds for row in draft.sections], [['f3'], ['f1', 'f2']])
        self.assertTrue(draft.sections[-1].analysis)

    async def test_different_filters_do_not_inherit_headline_in_same_book_and_window(self):
        payload = payload_for_composition()
        goal = payload['understanding']['requestedAnswers'][0]
        payload['facts'] = [
            bound_fact('f1', '目标分类的条件预测。', 'g1', 'forecast', headline=True, filters=[{'category': '工资'}]),
            bound_fact('f2', '另一分类的历史来源。', 'g2', 'sources', filters=[{'category': '稿费'}]),
        ]
        fake = FakeComposition(section('统计口径', goal, 'f2'))
        draft = await compose_fixture(payload, fake.invoke)
        self.assertEqual([row.factIds for row in draft.sections], [['f2'], ['f1']])
        self.assertTrue(draft.sections[-1].analysis)

    async def test_headline_dependency_over_capacity_raises_without_discarding_facts(self):
        payload = payload_for_composition()
        goal = payload['understanding']['requestedAnswers'][0]
        payload['facts'] = [bound_fact('f1', '预测合计。', 'g1', 'forecast', headline=True)] + [
            bound_fact(f'f{i}', '来源预测贡献。', 'g1', 'forecast') for i in range(2, 10)
        ]
        fake = FakeComposition(section('收入构成', goal, *(f'f{i}' for i in range(2, 10))))
        with self.assertRaisesRegex(ValueError, '主结论.*8'):
            await compose_fixture(payload, fake.invoke)

    async def test_group_wider_than_eight_keeps_all_facts_and_explains_once(self):
        payload = payload_for_composition()
        goal = payload['understanding']['requestedAnswers'][0]
        payload['facts'] = [bound_fact(f'f{i}', '某条已算结果。', 'g1', 'aggregate') for i in range(1, 11)]
        first = [f'f{i}' for i in range(1, 7)]
        second = [f'f{i}' for i in range(7, 11)]
        fake = FakeComposition(section('变化来源', goal, *first), section('统计口径', goal, *second))
        draft = await compose_fixture(payload, fake.invoke)
        self.assertEqual([row.factIds for row in draft.sections], [first, second])
        self.assertTrue(draft.sections[0].analysis)
        self.assertEqual(draft.sections[1].analysis, '')

    async def test_duplicate_evidence_sections_only_explain_the_group_once(self):
        payload = payload_for_composition()
        goals = ['解释哪些类别增加', '核对统计口径']
        payload['understanding']['requestedAnswers'] = goals
        fake = FakeComposition(section('变化来源', goals[0], 'f1'), section('统计口径', goals[1], 'f1'))
        draft = await compose_fixture(payload, fake.invoke)
        self.assertEqual([row.factIds for row in draft.sections], [['f1']])
        self.assertEqual(draft.sections[0].analysis.count('本组账单已核对。'), 1)


class GroundedAnalysisTests(unittest.TestCase):
    def test_fallback_helper_validates_scope_even_for_seen_groups(self):
        fact = bound_fact('f1', '原始账本事实。', 'g1', 'changes')
        findings = {'g1': {'tool': 'changes', 'scope': '家庭版', 'observations': ['不同账本的发现。']}}
        seen = {'g1'}
        with self.assertRaisesRegex(ValueError, '范围'):
            grounded_analysis(['f1'], [fact], findings, seen)

    def test_fallback_helper_deduplicates_across_calls(self):
        fact = bound_fact('f1', '原始账本事实。', 'g1', 'changes')
        findings = {'g1': {'tool': 'changes', 'scope': '个人版', 'observations': ['本范围已核对。']}}
        seen = set()
        self.assertEqual(grounded_analysis(['f1'], [fact], findings, seen), '【个人版】本范围已核对。')
        self.assertEqual(grounded_analysis(['f1'], {'f1': fact}, findings, seen), '')
        self.assertEqual(seen, {'g1'})


if __name__ == '__main__':
    unittest.main()
