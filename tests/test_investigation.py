import unittest
from unittest.mock import AsyncMock

from app.schemas.plan import AgentPlan


def observation(key, name, member='member_a', metric='income', excluded=False, remarks=None):
    return {'sourceKey': key, 'source': name, 'memberKey': member, 'member': '李明',
            'metric': metric, 'excluded': excluded, 'amount': 50,
            'remarks': [name] if remarks is None else remarks}


class InvestigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_group_salvages_project_stages_and_leaves_the_independent_salary_named(self):
        from app.agent.investigation import EvidenceInvestigator, InvestigationDecision
        rows = [observation(str(i), name) for i, name in enumerate(
            ('青石编辑部月结工资', '晚风图集初稿', '晚风图集修改', '晚风图集交付'))]
        catalogs = [{'mode': '个人版', 'sources': rows}]
        mixed = {'label': '常规项目收入（含青石编辑部和晚风图集）', 'sourceKeys': ['k1', 'k2', 'k3', 'k4']}
        agent = EvidenceInvestigator()
        agent._invoke = AsyncMock(return_value=InvestigationDecision(sourceGroups=[mixed]))
        decision, audit = await agent.refine('收入由哪些工作组成？', '', AgentPlan(), catalogs)
        self.assertEqual([(g.label, g.sourceKeys) for g in decision.sourceGroups], [('晚风图集', ['1', '2', '3'])])
        self.assertTrue(audit['issues'])

    async def test_stale_group_is_regrounded_even_when_reflection_times_out(self):
        from app.agent.investigation import EvidenceInvestigator
        rows = [observation('s', '青石编辑部月结'), observation('a', '晚风图集初稿'), observation('b', '晚风图集交付')]
        plan = AgentPlan(sourceGroups=[{'label': '常规项目收入', 'sourceKeys': ['s', 'a', 'b']}])
        agent = EvidenceInvestigator()
        agent._invoke = AsyncMock(side_effect=TimeoutError())
        _, audit = await agent.refine('这个预测怎么算的？', '', plan, [{'mode': '个人版', 'sources': rows}])
        self.assertEqual([(g.label, g.sourceKeys) for g in plan.sourceGroups], [('晚风图集', ['a', 'b'])])
        self.assertTrue(audit['issues'])

    async def test_refined_stage_alias_reconciles_the_individual_forecast_contributions(self):
        from datetime import date
        from app.agent.source_grouping import grounded_source_groups
        from app.schemas.plan import SourceGroup
        from app.tools.financial_analysis import run_analysis
        today = date(2026, 11, 20)
        records = []
        for month, stage in enumerate(('起稿', '校样', '送印'), 8):
            for amount, remark in [(3200, '东山剧社月结'), (500, '湖岸画册' + stage)]:
                records.append({'year': 2026, 'month': month, 'day': 1, 'money': amount,
                                'orderRemark': remark, 'costType': '收入', 'memberKey': 'member_a'})
        catalog = run_analysis('sources', records, as_of=today)
        keys = [row['sourceKey'] for row in catalog['sources']]
        groups, _ = grounded_source_groups([SourceGroup(label='综合收入', sourceKeys=keys)], [catalog], '各项收入分别是多少？')
        forecast = run_analysis('forecast', records, as_of=today, metric='income', target_month='2027-01',
                                source_groups=[group.model_dump() for group in groups])
        self.assertEqual(forecast['estimate'], 3700)
        self.assertEqual({row['source']: row['estimate'] for row in forecast['sourceForecasts']},
                         {'东山剧社月结': 3200, '湖岸画册': 500})

    async def test_short_named_business_with_generic_label_suffix_requires_remark_support_on_each_source(self):
        from app.agent.source_grouping import grounded_source_groups
        from app.schemas.plan import SourceGroup
        rows = [observation('a', '训练费', remarks=['带球培训']), observation('b', '课酬', remarks=['带球比赛']),
                observation('c', '带球', remarks=['无'])]
        groups, _ = grounded_source_groups([SourceGroup(label='带球收入', sourceKeys=['a', 'b', 'c'])],
                                           [{'sources': rows}], '收入怎么组成？')
        self.assertEqual([(g.label, g.sourceKeys) for g in groups], [('带球', ['a', 'b'])])

    async def test_member_direction_exclusion_and_generic_text_are_not_business_evidence(self):
        from app.agent.investigation import EvidenceInvestigator, InvestigationDecision
        rows = [observation('a', '和光展览策划'), observation('b', '和光展览搭建'),
                observation('c', '和光展览搭建', member='member_b'),
                observation('d', '和光展览搭建', metric='expense'),
                observation('e', '和光展览搭建', excluded=True),
                observation('f', '2026年8月 李明 项目收入 500元'), observation('g', '2026年8月 李明 工资收入 500元')]
        agent = EvidenceInvestigator()
        agent._invoke = AsyncMock(return_value=InvestigationDecision(sourceGroups=[
            {'label': '李明收入', 'sourceKeys': ['k1', 'k2', 'k3', 'k4', 'k5']},
            {'label': '项目收入', 'sourceKeys': ['k6', 'k7']}]))
        decision, audit = await agent.refine('按成员和业务查看', '', AgentPlan(), [{'mode': '家庭版', 'sources': rows}])
        self.assertEqual([(g.label, g.sourceKeys) for g in decision.sourceGroups], [('和光展览', ['a', 'b'])])
        self.assertTrue(audit['issues'])

    async def test_single_source_and_unmentioned_short_match_keep_observed_names(self):
        from app.agent.investigation import EvidenceInvestigator, InvestigationDecision
        rows = [observation('a', '周末授课', remarks=['带球训练费']), observation('b', '俱乐部', remarks=['教练带球']),
                observation('c', '编辑部月结')]
        agent = EvidenceInvestigator()
        agent._invoke = AsyncMock(return_value=InvestigationDecision(sourceGroups=[
            {'label': '固定收入', 'sourceKeys': ['k1', 'k2']}, {'label': '终身工资', 'sourceKeys': ['k3']}]))
        decision, _ = await agent.refine('查看各来源', '', AgentPlan(), [{'mode': '个人版', 'sources': rows}])
        self.assertEqual(decision.sourceGroups, [])

    async def test_disambiguated_member_name_is_not_a_business_name_or_an_implicit_rename(self):
        from app.agent.source_grouping import grounded_source_groups
        from app.schemas.plan import SourceGroup
        rows = [dict(observation('a', '高晓宁 家教课酬'), member='高晓宁（成员1）'),
                dict(observation('b', '高晓宁 稿件尾款'), member='高晓宁（成员1）'),
                observation('c', '每周课酬')]
        groups = [SourceGroup(label='高晓宁收入', sourceKeys=['a', 'b']),
                  SourceGroup(label='课程', sourceKeys=['c'])]
        kept, _ = grounded_source_groups(groups, [{'sources': rows}], '把每周课酬称为课程培训')
        self.assertEqual(kept, [])

    async def test_source_selectors_resolve_observations_and_groups_or_skip_the_entire_task(self):
        from app.agent.investigation import EvidenceInvestigator, InvestigationDecision
        plan = AgentPlan.model_validate({'toolCalls': [{'id': 'ledger', 'toolName': 'search_personal_orders'}],
            'sourceGroups': [{'label': '原计划项目', 'sourceKeys': ['observed_c']}]})
        sources = [{'sourceKey': f'observed_{key}', 'source': label, 'amount': amount, 'metric': 'income'}
                   for key, label, amount in [('a', '稿酬', 100), ('b', '奖金', 50), ('c', '实习', 35)]]
        filters = [
            {'includeSources': ['k1', '稿酬', '课程合计', '原计划项目'], 'excludeSources': ['k2']},
            {'includeSources': ['k1', 'k99']}, {'excludeSources': ['凭空编的']},
            {'includeSources': ['observed_c']}]
        investigator = EvidenceInvestigator()
        investigator._invoke = AsyncMock(return_value=InvestigationDecision.model_validate({
            'sourceGroups': [{'label': '课程合计', 'sourceKeys': ['k1']}],
            'additionalAnalyses': [{'tool': 'aggregate', 'queryIds': ['ledger'],
                'aggregate': {'filters': selectors}} for selectors in filters]}))
        decision, audit = await investigator.refine('把稿酬称为课程合计，把实习称为原计划项目，按来源核对', '', plan,
                                                   [{'mode': '个人版', 'sources': sources}])
        accepted = [task.aggregate.filters for task in decision.additionalAnalyses]
        self.assertEqual(len(accepted), 2)
        self.assertEqual(accepted[0].includeSources, ['observed_a', '稿酬', '课程合计', '原计划项目'])
        self.assertEqual(accepted[0].excludeSources, ['observed_b'])
        self.assertEqual(accepted[1].includeSources, ['observed_c'])
        self.assertEqual(len(audit['issues']), 2)

    async def test_supplemental_aggregate_does_not_widen_a_requested_filter_with_a_placeholder(self):
        from app.agent.investigation import EvidenceInvestigator, InvestigationDecision
        plan = AgentPlan(toolCalls=[{'id': 'ledger', 'toolName': 'search_personal_orders'}],
                         analyses=[{'tool': 'aggregate', 'aggregate': {'filters': {'includeCategories': ['展览']}}}])
        agent = EvidenceInvestigator()
        agent._invoke = AsyncMock(return_value=InvestigationDecision(additionalAnalyses=[
            {'tool': 'aggregate', 'queryIds': ['ledger'], 'aggregate': {
                'groupBy': ['weekday'], 'filters': {'includeCategories': ['all'], 'includeMembers': ['不限']}}}]))
        decision, audit = await agent.refine('只看展览，按星期核对。', '', plan, [])
        self.assertEqual(decision.additionalAnalyses, [])
        self.assertTrue(audit['issues'])

    async def test_aggregate_dates_are_json_safe_in_model_payload_and_saved_audit(self):
        import json
        from app.agent.investigation import EvidenceInvestigator, InvestigationDecision
        spec = {'periods': [{'label': '十月', 'startDate': '2026-10-01', 'endDate': '2026-10-31'}]}
        plan = AgentPlan.model_validate({'toolCalls': [{'id': 'ledger', 'toolName': 'search_personal_orders'}],
            'analyses': [{'tool': 'aggregate', 'aggregate': spec}]})
        investigator = EvidenceInvestigator()
        investigator._invoke = AsyncMock(return_value=InvestigationDecision.model_validate({'additionalAnalyses': [
            {'tool': 'aggregate', 'queryIds': ['ledger'], 'aggregate': spec}]}))
        _, audit = await investigator.refine('按月比较', '', plan, [])
        json.dumps(investigator._invoke.await_args.args[0])
        self.assertIn('2026-10-01', json.dumps(audit))

    async def test_source_in_family_catalog_cannot_be_used_as_a_personal_query_selector(self):
        from app.agent.investigation import EvidenceInvestigator, InvestigationDecision
        investigator = EvidenceInvestigator()
        analyst = {'tool': 'aggregate', 'aggregate': {'filters': {'excludeSources': ['k2']}}}
        investigator._invoke = AsyncMock(return_value=InvestigationDecision.model_validate({
            'additionalAnalyses': [{**analyst, 'queryIds': [key]} for key in ('mine', 'family')]}))
        plan = AgentPlan.model_validate({'toolCalls': [{'id': 'mine', 'toolName': 'search_personal_orders'},
                                                      {'id': 'family', 'toolName': 'search_family_orders'}]})
        catalogs = [{'mode': mode, 'sources': [{'source': name, 'sourceKey': key, 'amount': 50, 'metric': 'income'}]}
                    for mode, name, key in [('个人版', '稿费', 'observed_a'), ('家庭版', '项目', 'observed_b')]]
        decision, audit = await investigator.refine('核对个人和家庭来源', '', plan, catalogs)
        self.assertEqual([task.queryIds for task in decision.additionalAnalyses], [['family']])
        self.assertEqual(decision.additionalAnalyses[0].aggregate.filters.excludeSources, ['observed_b'])
        self.assertEqual(len(audit['issues']), 1)

    async def test_invalid_model_structure_is_repaired_once_using_the_same_evidence(self):
        from app.agent.investigation import EvidenceInvestigator, InvestigationDecision
        investigator = EvidenceInvestigator()
        investigator._invoke = AsyncMock(side_effect=[ValueError('sourceGroups 必须是数组'),
            InvestigationDecision(sourceGroups=[])])
        decision, audit = await investigator.refine('核对收入来源', '', AgentPlan(), [])
        self.assertEqual(audit['status'], 'refined')
        self.assertTrue(audit['issues'])

    async def test_observed_remarks_can_be_grouped_without_inventing_sources(self):
        from app.agent.investigation import EvidenceInvestigator, InvestigationDecision
        investigator = EvidenceInvestigator()
        investigator._invoke = AsyncMock(return_value=InvestigationDecision.model_validate({
            'sourceGroups': [{'label': '带球', 'sourceKeys': ['k1', 'k2']}],
            'additionalAnalyses': [{'tool': 'sources', 'queryIds': ['ledger']}],
            'answerFocus': '将周末训练费和篮球授课费合并，解释收入构成。',
        }))
        plan = AgentPlan.model_validate({'toolCalls': [{'id': 'ledger', 'toolName': 'search_personal_orders'}],
            'analyses': [{'tool': 'forecast'}]})
        catalogs = [{'mode': '个人版', 'sources': [
            {'sourceKey': 'observed_a', 'source': '周末训练费', 'member': '我', 'metric': 'income', 'amount': 1200, 'remarks': ['带球训练费']},
            {'sourceKey': 'observed_b', 'source': '篮球授课费', 'member': '我', 'metric': 'income', 'amount': 800, 'remarks': ['教练带球']},
        ]}]
        decision, audit = await investigator.refine('把带球收入单列出来', '助手: 下月预计有带球与实习收入。', plan, catalogs)
        self.assertEqual(decision.sourceGroups[0].sourceKeys, ['observed_a', 'observed_b'])
        self.assertEqual(audit['status'], 'refined')

    async def test_unknown_source_and_query_cannot_become_executable(self):
        from app.agent.investigation import EvidenceInvestigator, InvestigationDecision
        investigator = EvidenceInvestigator()
        investigator._invoke = AsyncMock(return_value=InvestigationDecision.model_validate({
            'sourceGroups': [{'label': '假来源', 'sourceKeys': ['invented']}],
            'additionalAnalyses': [{'tool': 'sources', 'queryIds': ['foreign']}],
        }))
        plan = AgentPlan.model_validate({'toolCalls': [{'id': 'ledger', 'toolName': 'search_personal_orders'}]})
        decision, audit = await investigator.refine('解释来源', '', plan, [])
        self.assertEqual(decision.sourceGroups, [])
        self.assertEqual(decision.additionalAnalyses, [])
        self.assertTrue(audit['issues'])

    async def test_timeout_preserves_existing_analysis_instead_of_erasing_evidence(self):
        from app.agent.investigation import EvidenceInvestigator
        investigator = EvidenceInvestigator()
        investigator._invoke = AsyncMock(side_effect=TimeoutError())
        decision, audit = await investigator.refine('分析备注', '', AgentPlan(), [])
        self.assertEqual(decision.additionalAnalyses, [])
        self.assertEqual(audit['status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
