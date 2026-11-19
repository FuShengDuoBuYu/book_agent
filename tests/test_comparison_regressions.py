"""Replay saved fictional Qwen plans without invoking a model or a ledger API."""

import json
import unittest
from copy import deepcopy
from datetime import date
from unittest.mock import AsyncMock

from app.agent.analysis_context import latest_context
from app.agent.book_agent import BookAgent
from app.agent.investigation import merged_source_groups
from app.agent.source_grouping import grounded_source_groups
from app.schemas.plan import AgentPlan, SourceGroup
from app.tools.financial_analysis import run_analysis


TODAY = date(2026, 11, 19)
STAGES = ('星港海报初排', '星港海报调版', '星港海报完稿')
# Public opaque keys and context from qwen-r1 forecast_followups turns 1 and 2.
# The recorded model selected net instead of income; retaining that value keeps
# this regression about the independently observed grouping failure.
SAVED_GROUP = {'label': '星港海报', 'sourceKeys': [
    'src_45b9de927f6fcb99e99da92b', 'src_cdc8bad5b7e959701e99501c',
    'src_e858bdefcc25ab8eae770d7a']}
SAVED_ANALYSIS = {'tool': 'forecast', 'queryIds': ['ledger'], 'metric': 'net',
                  'targetMonth': '2027-01', 'months': 1}
SAVED_CONTEXT = {
    'targetMonth': '2027-01', 'months': 1, 'changes': [], 'protectedCategories': [],
    'scope': [['search_personal_orders', '不限', '']],
    'activeAnalyses': [SAVED_ANALYSIS], 'activeHistoryWindow': None,
    'conversation': {
        'objective': '预测 2027 年 1 月的预计进账金额及收入构成',
        'analyses': [SAVED_ANALYSIS],
        'queries': [{'id': 'ledger', 'toolName': 'search_personal_orders',
                     'args': {'cost_type': '不限', 'remark': ''}}],
        'analysisWindow': None, 'sourceGroups': [SAVED_GROUP],
        'output': {'table': False, 'chart': 'none', 'metric': 'income', 'export': False},
    },
}
SAVED_PARTIAL_REFINEMENT = {'label': '星港海报',
                            'sourceKeys': SAVED_GROUP['sourceKeys'][:2]}


def replay_records():
    """Only the fictional observations involved in the saved grouping decision."""
    return [{'year': 2026, 'month': month, 'day': 5, 'money': 500,
             'costType': '项目收入', 'orderRemark': STAGES[index % 3],
             'memberKey': 'member_64bd40636a6a8c471985afba', 'memberName': '晴禾'}
            for index, month in enumerate(range(5, 11))]


class ComparisonRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_saved_partial_refinement_preserves_all_confirmed_project_stages_and_context(self):
        records = replay_records()
        catalog = run_analysis('sources', records, as_of=TODAY)
        self.assertEqual({row['sourceKey'] for row in catalog['sources']}, set(SAVED_GROUP['sourceKeys']))
        plan = AgentPlan.model_validate({
            'intent': 'analyze_orders', 'analysisType': 'financial_analysis',
            'followUpOfPreviousQuestion': True,
            'understanding': {'objective': '将 2027-01 的预测进账金额按来源拆解',
                              'evidenceNeeds': ['sources', 'forecast']},
            'output': {'table': True},
            'toolCalls': SAVED_CONTEXT['conversation']['queries'],
            'analyses': [SAVED_ANALYSIS], 'sourceGroups': [SAVED_GROUP],
        })
        plan._analysis_context = deepcopy(SAVED_CONTEXT)
        proposal = [SourceGroup.model_validate(SAVED_PARTIAL_REFINEMENT)]
        merged = merged_source_groups(plan.sourceGroups, proposal)
        plan.sourceGroups, _ = grounded_source_groups(
            merged, [catalog], '把刚才预测的那个数按每个项目拆出来，做个表，合计也要有。')
        result = run_analysis('forecast', records, as_of=TODAY, metric=plan.analyses[0].metric,
                              target_month=plan.analyses[0].targetMonth,
                              source_groups=[group.model_dump() for group in plan.sourceGroups])
        project, = [row for row in result['sourceForecasts'] if row['source'] == '星港海报']
        self.assertEqual(set(project['remarks']), set(STAGES))
        self.assertEqual((project['estimate'], project['lower'], project['upper']), (500, 500, 500))
        self.assertEqual(len(result['sourceForecasts']), 1)

        agent = BookAgent.__new__(BookAgent)
        agent.memory = AsyncMock()
        await agent._save_assistant_reply('fictional', 'fictional', '已计算', plan)
        stored = latest_context(json.loads(json.dumps([agent.memory.add_message.await_args.kwargs])))
        self.assertEqual(stored['scope'], SAVED_CONTEXT['scope'])
        self.assertEqual(stored['activeAnalyses'][0]['metric'], 'net')
        self.assertEqual(stored['targetMonth'], '2027-01')
        group, = stored['conversation']['sourceGroups']
        self.assertEqual(set(group['sourceKeys']), set(SAVED_GROUP['sourceKeys']))

    async def test_same_label_fragments_still_respect_member_direction_and_exclusion_boundaries(self):
        rows = [
            {'sourceKey': key, 'source': name, 'remarks': [name], 'memberKey': member,
             'metric': metric, 'excluded': excluded}
            for key, name, member, metric, excluded in [
                ('a', '星港海报初排', 'member_a', 'income', False),
                ('b', '星港海报完稿', 'member_a', 'income', False),
                ('c', '星港海报调版', 'member_b', 'income', False),
                ('d', '星港海报材料', 'member_a', 'expense', False),
                ('e', '星港海报一次性出售', 'member_a', 'income', True),
            ]]
        existing = [SourceGroup(label='星港海报', sourceKeys=['a', 'b', 'c', 'd', 'e'])]
        groups, _ = grounded_source_groups(
            merged_source_groups(existing, [SourceGroup(label='星港海报', sourceKeys=['a', 'b'])]),
            [{'sources': rows}], '把刚才的项目按成员拆开。')
        self.assertEqual([(group.label, group.sourceKeys) for group in groups], [('星港海报', ['a', 'b'])])


if __name__ == '__main__':
    unittest.main()
