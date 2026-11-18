import unittest
from unittest.mock import AsyncMock

from app.schemas.plan import AgentPlan


class InvestigationTests(unittest.IsolatedAsyncioTestCase):
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
        decision, audit = await investigator.refine('按来源核对', '', plan, [{'mode': '个人版', 'sources': sources}])
        accepted = [task.aggregate.filters for task in decision.additionalAnalyses]
        self.assertEqual(len(accepted), 2)
        self.assertEqual(accepted[0].includeSources, ['observed_a', '稿酬', '课程合计', '原计划项目'])
        self.assertEqual(accepted[0].excludeSources, ['observed_b'])
        self.assertEqual(accepted[1].includeSources, ['observed_c'])
        self.assertEqual(len(audit['issues']), 2)

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
