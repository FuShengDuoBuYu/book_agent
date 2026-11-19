import unittest
from argparse import Namespace
from unittest.mock import AsyncMock, patch

from scripts import evaluate_acceptance


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
