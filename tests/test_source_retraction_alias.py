"""Offline replay of Terra's saved source condition and empty reviewed changes."""

import json
import unittest
from copy import deepcopy
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent.analysis_context import latest_context
from app.agent.book_agent import BookAgent
from app.agent.evidence_pipeline import collect_analysis_results
from app.agent.plan_review import PlanReviewer
from app.agent.planner import QueryPlanner
from app.schemas.plan import AgentPlan
from app.tools.source_analysis import observed_source_key


TODAY = date(2026, 11, 19)
QUESTION = '不，撤回“星港少一半”这个假设，恢复原来的收入，三个月能新增多少？'
QUERY = {'id': 'ledger', 'toolName': 'search_personal_orders'}
WINDOW = {'startDate': '2026-08-01', 'endDate': '2026-10-31'}
OLD_CHANGE = {'metric': 'income', 'source': '星港海报', 'percent': -50, 'label': '用户假设'}
SOURCE_TASK = {'tool': 'sources', 'queryIds': ['ledger'], 'metric': 'all'}
PROJECTION = {'tool': 'projection', 'queryIds': ['ledger'], 'metric': 'net',
              'targetMonth': '2026-12', 'months': 3}


def saved_context(changes, groups=()):
    task = {**PROJECTION, 'changes': changes}
    return deepcopy({
        'scope': [['search_personal_orders', '不限', '']],
        'months': 3, 'targetMonth': '2026-12', 'changes': changes, 'protectedCategories': [],
        'activeAnalyses': [task], 'activeHistoryWindow': WINDOW,
        'conversation': {'queries': [QUERY], 'analyses': [SOURCE_TASK, task],
                         'analysisWindow': WINDOW, 'sourceGroups': list(groups)},
    })


def projection_rows():
    return [
        {'id': f'{month}-{index}', 'year': 2026, 'month': month, 'day': 4,
         'money': amount, 'costType': category, 'orderRemark': remark}
        for month, phase in zip((8, 9, 10), ('初排', '调版', '完稿'))
        for index, (amount, category, remark) in enumerate(
            [(3200, '工资', '河畔编辑部月结'), (500, '稿费', '星港海报' + phase),
             (-1680, '房租', '公寓租金')])
    ]


def projection_evidence(plan, rows):
    results = collect_analysis_results(plan, {'个人版': [(call, rows) for call in plan.toolCalls]}, TODAY)
    result, = [row['evidence'] for row in results if row['tool'] == 'projection']
    return result


class SourceRetractionAliasTests(unittest.IsolatedAsyncioTestCase):
    async def make_plan(self, question, prior, *, review=False):
        tasks = [SOURCE_TASK, {**PROJECTION, 'changes': []}]
        draft = {'intent': 'analyze_orders', 'analysisType': 'financial_analysis',
                 'followUpOfPreviousQuestion': True, 'toolCalls': [QUERY],
                 'analysisWindow': WINDOW, 'analyses': tasks}
        planner = QueryPlanner.__new__(QueryPlanner)
        planner.chain = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(
            content=json.dumps(draft, ensure_ascii=False))))
        reviewer_output = {'adequate': True, 'issues': [], 'analysisWindow': WINDOW,
                           'answerInstruction': '根据重新测算的结果回答累计新增结余。', 'analyses': tasks}
        reviewer_model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(
            content=json.dumps(reviewer_output, ensure_ascii=False))))
        if review:
            planner.reviewer = PlanReviewer()
        with (patch('app.agent.planner.datetime', wraps=datetime) as clock,
              patch('app.agent.plan_review.get_structured_model', return_value=reviewer_model)):
            clock.now.return_value = datetime(2026, 11, 19)
            return await planner.create_plan(question, '', '个人版', prior)

    async def test_shortened_source_retraction_restores_projection_and_survives_reload(self):
        rows = projection_rows()
        group = {'label': '星港海报', 'sourceKeys': [
            observed_source_key(row) for row in rows if row['orderRemark'].startswith('星港海报')]}
        prior = saved_context([OLD_CHANGE], [group])
        prior['observedSourceDirections'] = {'星港海报': ['income'], '河畔编辑部月结': ['income']}
        plan = await self.make_plan(QUESTION, prior, review=True)
        self.assertEqual(plan._semantic_review['status'], 'reviewed')
        self.assertEqual([group.model_dump() for group in plan.sourceGroups], [group])
        result = projection_evidence(plan, rows)
        self.assertEqual((result['monthlyIncome'], result['totalNet']), (3700, 6060))
        self.assertEqual(result['assumptions'], [])

        agent = BookAgent.__new__(BookAgent)
        agent.memory = AsyncMock()
        await agent._save_assistant_reply('fictional', 'fictional', '已计算', plan)
        message = agent.memory.add_message.await_args.kwargs
        stored = latest_context(json.loads(json.dumps([message], ensure_ascii=False)))
        self.assertEqual(stored['changes'], [])
        self.assertTrue(all(task['changes'] == [] for task in stored['activeAnalyses']))
        self.assertTrue(all(task['changes'] == [] for task in stored['conversation']['analyses']))

        follow = await self.make_plan('按刚才的假设和期限，再算一次。', stored)
        result = projection_evidence(follow, rows)
        self.assertEqual((result['months'], result['totalNet'], result['assumptions']), (3, 6060, []))

    def test_explicit_direction_retracts_only_its_bound_change_and_prunes_cached_tasks(self):
        same_source_expense = {'metric': 'expense', 'source': '星港海报', 'percent': 15}
        other_income = {'metric': 'income', 'source': '河畔编辑部月结', 'percent': -20}
        prior = saved_context([OLD_CHANGE, same_source_expense, other_income])
        plan = AgentPlan.model_validate({'followUpOfPreviousQuestion': True, 'toolCalls': [QUERY],
                                        'analyses': [SOURCE_TASK]})
        planner = QueryPlanner.__new__(QueryPlanner)
        planner._sanitize_conditions(plan, '撤销“星港收入少一半”这个假设，其他条件照旧。', '个人版', prior)
        expected = [same_source_expense, other_income]
        self.assertEqual(prior['changes'], expected)
        self.assertEqual(prior['activeAnalyses'][0]['changes'], expected)
        self.assertEqual(prior['conversation']['analyses'][1]['changes'], expected)

    def test_ambiguous_alias_keeps_each_candidate(self):
        for other in ({'metric': 'income', 'source': '星港策展', 'percent': -50},
                      {'metric': 'expense', 'source': '星港海报', 'percent': -50}):
            with self.subTest(other=other):
                prior = saved_context([OLD_CHANGE, other])
                plan = AgentPlan.model_validate({'followUpOfPreviousQuestion': True,
                                                'toolCalls': [QUERY], 'analyses': [PROJECTION]})
                QueryPlanner.__new__(QueryPlanner)._sanitize_conditions(
                    plan, '撤回“星港少一半”这个假设。', '个人版', prior)
                self.assertEqual(prior['changes'], [OLD_CHANGE, other])

    def test_negation_and_historical_mentions_preserve_prior_condition(self):
        for question in (
            '别取消“星港海报少一半”这个假设，继续推演。',
            '我没有撤回“星港海报少一半”这个假设，继续推演。',
            '我之前提到过撤回“星港海报少一半”这个假设，这次只是回顾那句话。',
            '如果取消“星港海报少一半”这个假设会怎样？',
            '我不是要撤回星港海报少一半这个假设，继续推演。',
            '上一轮曾按正常星港海报算，这次只是核对说法。',
        ):
            with self.subTest(question=question):
                prior = saved_context([OLD_CHANGE])
                plan = AgentPlan.model_validate({'followUpOfPreviousQuestion': True,
                                                'toolCalls': [QUERY], 'analyses': [PROJECTION]})
                QueryPlanner.__new__(QueryPlanner)._sanitize_conditions(plan, question, '个人版', prior)
                self.assertEqual(prior['changes'], [OLD_CHANGE])

    def test_retraction_cannot_prune_another_ledger_or_filter(self):
        for query in ({**QUERY, 'toolName': 'search_family_orders'},
                      {**QUERY, 'args': {'remark': '河畔编辑部'}}):
            with self.subTest(query=query):
                prior = saved_context([OLD_CHANGE])
                plan = AgentPlan.model_validate({'followUpOfPreviousQuestion': True,
                                                'toolCalls': [query], 'analyses': [PROJECTION]})
                QueryPlanner.__new__(QueryPlanner)._sanitize_conditions(plan, QUESTION, '个人版', prior)
                self.assertEqual(prior['changes'], [OLD_CHANGE])
