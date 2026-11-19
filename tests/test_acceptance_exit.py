import unittest
from argparse import Namespace
from unittest.mock import AsyncMock, patch

from scripts import evaluate_acceptance


class AcceptanceEvidenceTests(unittest.TestCase):
    def test_canonical_source_and_user_food_alias_are_accepted_only_with_applied_evidence(self):
        record = {'case': 'chain_3', 'reply': '合成分析', 'metadata': {
            'validation': {'status': 'reviewed'}, 'analysisContext': {
                'protectedCategories': ['房租', '吃饭'],
                'changes': [{'metric': 'income', 'source': '家教课时费', 'percent': -50}]},
            'evidence': {'analysisResults': [
                {'tool': 'optimization', 'evidence': {'excludedCategories': ['房租', '餐饮']}},
                {'tool': 'projection', 'evidence': {'assumptions': [{'metric': 'income', 'source': '家教课时费',
                     'percent': -50, 'sourceBaseline': 1600, 'totalChange': -800}]}}]}}}
        self.assertTrue(all(evaluate_acceptance.assess(record).values()))
        record['metadata']['analysisContext']['protectedCategories'] = []
        self.assertFalse(evaluate_acceptance.assess(record)['protectedRetained'])
        record['metadata']['analysisContext']['changes'][0]['source'] = ''
        self.assertFalse(evaluate_acceptance.assess(record)['sourceScenarioRetained'])


class AcceptanceExitTests(unittest.IsolatedAsyncioTestCase):
    async def test_any_failed_semantic_check_makes_batch_fail(self):
        with patch.object(evaluate_acceptance, 'QUESTIONS', [('probe', '合成测试')]), \
                patch.object(evaluate_acceptance, 'FOLLOWUPS', []), \
                patch.object(evaluate_acceptance, 'VARIANTS', []), \
                patch.object(evaluate_acceptance, 'make_agent', return_value=object()), \
                patch.object(evaluate_acceptance, 'run_case', new=AsyncMock(return_value={'automatedPass': False})):
            self.assertEqual(await evaluate_acceptance.main(Namespace(case=None, timeout=1)), 1)

    async def test_passing_semantic_checks_make_batch_succeed(self):
        with patch.object(evaluate_acceptance, 'QUESTIONS', [('probe', '合成测试')]), \
                patch.object(evaluate_acceptance, 'FOLLOWUPS', []), \
                patch.object(evaluate_acceptance, 'VARIANTS', []), \
                patch.object(evaluate_acceptance, 'make_agent', return_value=object()), \
                patch.object(evaluate_acceptance, 'run_case', new=AsyncMock(return_value={'automatedPass': True})):
            self.assertEqual(await evaluate_acceptance.main(Namespace(case=None, timeout=1)), 0)
