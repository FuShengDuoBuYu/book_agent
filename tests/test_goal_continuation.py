import json
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent.evidence_pipeline import collect_analysis_results
from app.agent.evidence_report import build_facts
from app.agent.plan_review import PlanReviewDecision
from app.agent.planner import QueryPlanner
from app.tools.source_analysis import observed_source_key


TODAY = date(2026, 11, 20)
QUESTION = '起点和总目标不变，不过我不设截止日期了。照原来的月结余，大概多久达到？不要替我加一个期限。'
QUERY = {'id': 'ledger', 'toolName': 'search_personal_orders'}
SUPPORT = [
    {'tool': 'sources', 'queryIds': ['ledger']},
    {'tool': 'aggregate', 'queryIds': ['ledger'], 'metric': 'income',
     'aggregate': {'groupBy': ['month', 'member', 'source']}},
    {'tool': 'aggregate', 'queryIds': ['ledger'], 'metric': 'expense',
     'aggregate': {'groupBy': ['month']}},
]
SAVINGS = {'tool': 'savings', 'queryIds': ['ledger'], 'metric': 'net'}


def fixtures():
    rows = [
        {'year': 2026, 'month': month, 'day': 5, 'money': amount,
         'costType': category, 'orderRemark': remark}
        for month, stage in [(8, '初排'), (9, '调版'), (10, '完稿')]
        for amount, category, remark in [
            (3200, '收入', '河畔编辑部月结'), (500, '项目收入', '星港海报' + stage),
            (-1680, '房租', '每月开支'),
        ]
    ]
    rows.append({'year': 2026, 'month': 10, 'day': 12, 'money': 8000,
                 'costType': '其他', 'orderRemark': '旧设备一次性出售'})
    group = {'label': '星港海报', 'sourceKeys': [
        observed_source_key(row) for row in rows if row['costType'] == '项目收入'
    ]}
    return rows, [group]


def prior_goal(months=4, starting=10000, target=20000):
    goal = {**SAVINGS, 'months': months, 'startingBalance': starting, 'targetAmount': target}
    window = {'startDate': '2026-08-01', 'endDate': '2026-10-31'}
    return {
        **{key: goal[key] for key in ('months', 'startingBalance', 'targetAmount')},
        'scope': [['search_personal_orders', '不限', '']],
        'activeAnalyses': [goal],
        'activeHistoryWindow': window,
        'conversation': {'queries': [QUERY], 'analyses': [goal], 'analysisWindow': window,
                         'sourceGroups': fixtures()[1]},
    }


class GoalContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def plan(self, question, previous, *, tasks=SUPPORT, query=QUERY, follow=True, reset=False,
                   review_tasks=None):
        draft = {
            'intent': 'analyze_orders',
            'analysisType': 'financial_analysis',
            'followUpOfPreviousQuestion': follow,
            'resetAnalysisContext': reset,
            'toolCalls': [query],
            'analyses': tasks,
            'understanding': {'objective': '按月结余估算目标达成时间',
                              'requestedAnswers': ['多久能够达到目标'],
                              'evidenceNeeds': ['sources', 'aggregate']},
        }
        planner = QueryPlanner.__new__(QueryPlanner)
        planner.chain = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(
            content=json.dumps(draft, ensure_ascii=False))))
        if review_tasks is not None:
            review = PlanReviewDecision.model_validate({
                'adequate': False, 'issues': ['需对齐证据口径'],
                'analysisWindow': None, 'analyses': review_tasks,
                'answerInstruction': '沿用原资金条件核算。',
            })
            planner.reviewer = SimpleNamespace(review=AsyncMock(return_value=(review, {'status': 'reviewed'})))
        with patch('app.agent.planner.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 20)
            return await planner.create_plan(question, '', '个人版', previous)

    async def test_deadline_retraction_restores_savings_after_an_adopted_support_only_review(self):
        previous = prior_goal()
        plan = await self.plan(QUESTION, previous, review_tasks=SUPPORT)
        self.assertTrue(plan._semantic_review['adopted'])
        savings = [task for task in plan.analyses if task.tool == 'savings']
        self.assertEqual(len(savings), 1)
        self.assertEqual((savings[0].startingBalance, savings[0].targetAmount, savings[0].months),
                         (10000, 20000, None))
        self.assertEqual(plan.analysisWindow.model_dump(mode='json'), previous['activeHistoryWindow'])
        self.assertEqual([group.model_dump() for group in plan.sourceGroups],
                         previous['conversation']['sourceGroups'])
        self.assertIsNone(plan._analysis_context.get('months'))

        rows, _ = fixtures()
        result, = [row for row in collect_analysis_results(
            plan, {'个人版': [(call, rows) for call in plan.toolCalls]}, TODAY) if row['tool'] == 'savings']
        evidence = result['evidence']
        self.assertEqual((evidence['baselineMonthlySaving'], evidence['estimatedMonthsToTarget']), (2020, 5))
        self.assertIsNone(evidence['requiredMonthlySaving'])
        self.assertTrue(any(fact['required'] and '约需 5 个月' in fact['text'] for fact in build_facts([result])))

    async def test_retracting_deadline_while_requesting_sources_does_not_add_savings(self):
        plan = await self.plan('取消截止日期，起点和目标不变，先列一下收入来源。', prior_goal())
        self.assertNotIn('savings', [task.tool for task in plan.analyses])
        self.assertIsNone(plan._analysis_context.get('months'))

    async def test_deadline_retraction_does_not_inherit_funds_across_scope_or_reset(self):
        for query, follow, reset in [
            ({**QUERY, 'args': {'remark': '展览'}}, True, False),
            ({**QUERY, 'toolName': 'search_family_orders'}, True, False),
            (QUERY, False, True),
        ]:
            with self.subTest(query=query, reset=reset):
                question = '切到家庭账本。' + QUESTION if query['toolName'] == 'search_family_orders' else QUESTION
                plan = await self.plan(question, prior_goal(), query=query, follow=follow, reset=reset)
                self.assertIsNone(plan._analysis_context.get('startingBalance'))
                self.assertIsNone(plan._analysis_context.get('targetAmount'))
                self.assertTrue(all(task.startingBalance is None and task.targetAmount is None
                                    for task in plan.analyses if task.tool == 'savings'))

    async def test_deadline_retraction_never_supplies_an_unknown_starting_balance(self):
        plan = await self.plan(QUESTION, prior_goal(starting=None), tasks=[*SUPPORT, SAVINGS])
        savings, = [task for task in plan.analyses if task.tool == 'savings']
        self.assertEqual((savings.startingBalance, savings.targetAmount, savings.months), (None, 20000, None))

    async def test_explicit_replacement_deadline_takes_priority_over_retraction(self):
        plan = await self.plan('起点和总目标不变，取消四个月截止日期，改为六个月内达到目标。',
                               prior_goal(), tasks=[*SUPPORT, {**SAVINGS, 'months': 4}])
        savings, = [task for task in plan.analyses if task.tool == 'savings']
        self.assertEqual((savings.startingBalance, savings.targetAmount, savings.months), (10000, 20000, 6))

    async def test_different_model_horizons_that_resolve_to_the_same_goal_are_one_task(self):
        plan = await self.plan(
            '你说压力小了，是我每月能留下的钱变多了，还是每月要完成的目标少了？把这两个数分清楚。',
            prior_goal(months=5, target=22000),
            tasks=[SUPPORT[0], {**SAVINGS, 'months': 5}, {**SAVINGS, 'months': 3}],
        )
        savings = [task for task in plan.analyses if task.tool == 'savings']
        self.assertEqual(len(savings), 1)
        self.assertEqual((savings[0].startingBalance, savings[0].targetAmount, savings[0].months),
                         (10000, 22000, 5))
        self.assertFalse(plan._analysis_context.get('ambiguous'))

    async def test_distinct_requested_savings_horizons_remain_two_scenarios(self):
        plan = await self.plan(
            '我现在有10000元，想存到22000元。分别按3个月和5个月算所需月结余。',
            {}, follow=False,
            tasks=[{**SAVINGS, 'startingBalance': 10000, 'targetAmount': 22000, 'months': months}
                   for months in (3, 5)],
        )
        self.assertEqual([task.months for task in plan.analyses if task.tool == 'savings'], [3, 5])
        self.assertTrue(plan._analysis_context.get('ambiguous'))


if __name__ == '__main__':
    unittest.main()
