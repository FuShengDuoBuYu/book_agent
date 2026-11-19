import unittest
from datetime import date

from app.agent.evidence_pipeline import collect_analysis_results
from app.schemas.plan import AgentPlan


class EvidenceBindingTests(unittest.TestCase):
    def test_implicit_queries_keep_actual_logical_ids_in_their_own_ledger(self):
        plan = AgentPlan.model_validate({'intent': 'analyze_orders', 'needsTools': True, 'toolCalls': [
            {'id': 'mine_sep', 'sourceIds': ['mine'], 'toolName': 'search_personal_orders', 'args': {'year': 2026, 'month': 9}},
            {'id': 'mine_oct', 'sourceIds': ['mine'], 'toolName': 'search_personal_orders', 'args': {'year': 2026, 'month': 10}},
            {'id': 'home', 'toolName': 'search_family_orders'}],
            'analyses': [{'tool': 'cashflow'}, {'tool': 'forecast', 'queryIds': ['mine'], 'metric': 'income'}]})
        records = [{'year': 2026, 'month': m, 'day': 1, 'money': 100, 'costType': '收入'} for m in (9, 10)]
        datasets = {'个人版': [(call, records) for call in plan.toolCalls[:2]],
                    '家庭版': [(plan.toolCalls[2], records)]}
        results = collect_analysis_results(plan, datasets, date(2026, 11, 19))
        personal = [r for r in results if r['mode'] == '个人版']
        family = [r for r in results if r['mode'] == '家庭版']
        self.assertTrue(personal and family)
        self.assertTrue(all(r['queryIds'] == ['mine'] for r in personal))
        self.assertTrue(all(r['queryIds'] == ['home'] for r in family))
