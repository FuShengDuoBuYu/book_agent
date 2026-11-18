import unittest
from unittest.mock import AsyncMock

from app.schemas.plan import AgentPlan


class InvestigationTests(unittest.IsolatedAsyncioTestCase):
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
