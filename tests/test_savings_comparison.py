import unittest
from copy import deepcopy
from datetime import date
from unittest.mock import AsyncMock

from app.agent.evidence_report import EvidenceReporter
from app.tools.financial_analysis import run_analysis

WINDOW = {'startDate': '2026-08-01', 'endDate': '2026-10-31'}
ROWS = [{'year': 2026, 'month': month, 'day': 4, 'money': amount,
         'costType': category, 'orderRemark': category}
        for month in (8, 9, 10) for amount, category in [(3700, '收入'), (-1680, '日常')]]


class SavingsComparisonTests(unittest.IsolatedAsyncioTestCase):
    async def answer(self, context, months, *, starting=10000, target=22000, window=WINDOW,
                     mode='个人版', groups=None, changes=None, rows=ROWS, duplicate=False):
        evidence = run_analysis('savings', rows, as_of=date(2026, 11, 20),
                                starting_balance=starting, target_amount=target, months=months,
                                changes=changes or [])
        result = {'tool': 'savings', 'mode': mode, 'queryIds': ['ledger'],
                  'historyWindow': deepcopy(window), 'filters': [{'category': '不限', 'remark': ''}],
                  'evidence': evidence}
        reporter = EvidenceReporter.__new__(EvidenceReporter)
        reporter._draft = AsyncMock(side_effect=ValueError('synthetic outline failure'))
        _, validation = await reporter.answer('请说明每月能力与目标要求及期限变化。', context,
                                               [result, deepcopy(result)] if duplicate else [result],
                                               analysis_scope={'sourceGroups': groups or []})
        return [f for f in validation['facts']
                if f['binding'].get('aggregation') == 'savings_deadline_comparison']

    async def test_extension_and_later_explanation_retain_verified_requirement_comparison(self):
        context = {}
        self.assertEqual(await self.answer(context, 3), [])
        for _ in range(2):
            comparisons = await self.answer(context, 5)
            self.assertEqual(len(comparisons), 1)
            comparison, = comparisons
            self.assertTrue(comparison['required'])
            for value in ('4,000', '2,400', '1,600', '2,020'):
                self.assertIn(value, comparison['text'])
            self.assertIn('能力未变', comparison['text'])
            self.assertIn('要求降低', comparison['text'])
            self.assertEqual(comparison['binding']['resultGroup'], 'g1')
            self.assertEqual(comparison['binding']['historyWindow'], WINDOW)

    async def test_changed_goal_balance_scope_window_group_or_assumption_drops_old_comparison(self):
        for override in [dict(starting=11000), dict(target=24000), dict(mode='家庭版'),
                         dict(window={'startDate': '2026-07-01', 'endDate': '2026-10-31'}),
                         dict(groups=[{'label': '另一个项目', 'sourceKeys': ['a', 'b']}]),
                         dict(changes=[{'metric': 'income', 'amount': 1000}]),
                         dict(rows=ROWS + [{'year': 2026, 'month': month, 'day': 7, 'money': -600,
                                           'costType': '医疗', 'orderRemark': '医疗'} for month in (8, 9, 10)])]:
            with self.subTest(override=override):
                context = {}
                await self.answer(context, 3)
                self.assertEqual(await self.answer(context, 5, **override), [])

    async def test_removed_deadline_clears_the_extension_reference(self):
        context = {}
        await self.answer(context, 3)
        self.assertTrue(await self.answer(context, 5))
        self.assertEqual(await self.answer(context, None), [])
        self.assertEqual(await self.answer(context, 5), [])

    async def test_ambiguous_multiple_goals_do_not_get_a_single_deadline_comparison(self):
        context = {}
        await self.answer(context, 3)
        self.assertEqual(await self.answer(context, 5, duplicate=True), [])
        self.assertEqual(await self.answer(context, 5), [])
    async def test_equal_net_with_changed_income_and_expense_is_a_new_baseline(self):
        context = {}
        await self.answer(context, 3)
        changed = [dict(row, money=row['money'] + (600 if row['money'] > 0 else -600)) for row in ROWS]
        self.assertEqual(await self.answer(context, 5, rows=changed), [])

    async def test_internal_deadline_snapshot_is_not_a_model_user_condition(self):
        from app.agent.conversation_context import compact_user_context
        context = {}
        await self.answer(context, 3)
        before = deepcopy(context)
        self.assertNotIn('savingsDeadlineReference', compact_user_context(context))
        self.assertEqual(context, before)
