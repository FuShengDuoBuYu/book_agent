import json
import unittest
from copy import deepcopy

from app.agent.conversation_context import compact_user_context


PERSONAL = ['search_personal_orders', '餐饮', '展览']
FAMILY = ['search_family_orders', '不限', '']
WINDOW = {'startDate': '2026-09-01', 'endDate': '2026-10-31'}


def logical_query(identifier, scope, **dates):
    tool, category, remark = scope
    return {'id': identifier, 'toolName': tool, 'args': {'cost_type': category, 'remark': remark, **dates}}


class CompactUserContextTests(unittest.TestCase):
    def test_model_view_reuses_canonical_conditions_and_collapses_expanded_queries(self):
        change = {'metric': 'income', 'source': '稿费', 'percent': -50, 'count': 1, 'amount': None}
        conditions = {'startingBalance': 0, 'targetAmount': 3800, 'targetMonth': '2027-01',
                      'months': 4, 'changes': [change], 'protectedCategories': ['房租', '交通']}
        task = {'tool': 'projection', 'metric': 'net', 'queryIds': ['ledger'], 'budgetAmount': None, **conditions}
        original = {**conditions, 'scope': [PERSONAL], 'activeAnalyses': [task], 'activeHistoryWindow': WINDOW,
                    'conversation': {'objective': '沿用两个月作参照，说明这个项目按原安排还能留下多少。',
                        'analysisWindow': WINDOW, 'analyses': [deepcopy(task)],
                        'queries': [
                            {**logical_query(f'expanded-{month}', PERSONAL, year=2026, month=month, day=None),
                             'sourceIds': ['ledger'], 'reason': '按计划展开月份'}
                            for month in (9, 10)
                        ],
                        'output': {'metric': 'net', 'table': True, 'chart': 'none', 'export': False},
                        'sourceGroups': [{'label': '星港海报项目', 'sourceKeys': [f'opaque-{i:030d}' for i in range(40)]}]}}
        before = deepcopy(original)
        view = compact_user_context(original)
        self.assertEqual(original, before)
        for key in ('startingBalance', 'targetAmount', 'targetMonth', 'months', 'protectedCategories'):
            self.assertEqual(view[key], original[key])
        self.assertEqual(view['changes'], [{'metric': 'income', 'source': '稿费', 'percent': -50, 'count': 1}])
        self.assertEqual(view['scope'], [PERSONAL])
        conv = view['conversation']
        self.assertEqual(conv['objective'], original['conversation']['objective'])
        self.assertEqual(conv['analysisWindow'], WINDOW)
        self.assertEqual(conv['output'], original['conversation']['output'])
        self.assertEqual(conv['queries'], [logical_query('ledger', PERSONAL)])
        self.assertEqual(conv['analyses'], [{'tool': 'projection', 'metric': 'net', 'queryIds': ['ledger']}])
        self.assertNotIn('activeAnalyses', view)
        self.assertEqual(conv['sourceLabels'], ['星港海报项目'])
        self.assertNotIn('opaque-', json.dumps(view, ensure_ascii=False))
        self.assertLess(len(json.dumps(view, ensure_ascii=False)), len(json.dumps(original, ensure_ascii=False)) / 2)
        conv['sourceLabels'].append('只修改模型视图')
        view['protectedCategories'].append('只修改模型视图')
        self.assertEqual(original, before)

    def test_scenarios_keep_each_scope_and_its_own_conditions(self):
        personal = {'scope': [PERSONAL], 'startingBalance': 3200, 'targetAmount': 10000, 'months': 6,
                    'changes': [{'metric': 'expense', 'source': '培训', 'percent': -20}]}
        family = {'scope': [FAMILY], 'startingBalance': 0, 'targetAmount': 0, 'months': 3, 'changes': [],
                  'protectedCategories': ['医疗']}
        analyses = [
            {'tool': 'projection', 'metric': 'net', 'queryIds': ['personal'], 'months': 6,
             'changes': personal['changes']},
            {'tool': 'projection', 'metric': 'net', 'queryIds': ['family'], 'months': 3,
             'startingBalance': 0, 'targetAmount': 0, 'protectedCategories': ['医疗']},
        ]
        view = compact_user_context({'ambiguous': True, 'scenarios': [personal, family],
            'activeAnalyses': analyses, 'conversation': {'objective': '比较两个账本各自的情景。',
                'analyses': deepcopy(analyses),
                'queries': [logical_query('personal', PERSONAL), logical_query('family', FAMILY)]}})
        self.assertTrue(view['ambiguous'])
        self.assertEqual(view['scenarios'], [personal, family])
        self.assertEqual(view['conversation']['analyses'], analyses)
        self.assertEqual(view['conversation']['queries'], [logical_query('personal', PERSONAL),
                                                          logical_query('family', FAMILY)])
        self.assertNotIn('activeAnalyses', view)
        self.assertNotIn('startingBalance', view)
        self.assertEqual(view['conversation']['objective'], '比较两个账本各自的情景。')

    def test_distinct_active_goal_retains_its_conditions_and_sample_window(self):
        active = [{'tool': 'forecast', 'metric': 'income', 'queryIds': ['ledger'],
                   'targetMonth': '2027-01', 'startingBalance': 0}]
        context = {'scope': [PERSONAL], 'targetMonth': '2027-02', 'activeAnalyses': active,
                   'activeHistoryWindow': {'startDate': '2026-07-01', 'endDate': '2026-08-31'},
                   'conversation': {'objective': '单看最近两个月来源。', 'analyses': [
                       {'tool': 'sources', 'metric': 'all', 'queryIds': ['ledger'], 'changes': [],
                        'protectedCategories': []}],
                       'analysisWindow': WINDOW, 'queries': [logical_query('ledger', PERSONAL)]}}
        view = compact_user_context(context)
        self.assertEqual(view['activeAnalyses'], active)
        self.assertEqual(view['activeHistoryWindow'], context['activeHistoryWindow'])
        self.assertEqual(view['conversation']['analysisWindow'], WINDOW)
        self.assertEqual(view['targetMonth'], '2027-02')
        self.assertEqual(view['conversation']['analyses'],
                         [{'tool': 'sources', 'metric': 'all', 'queryIds': ['ledger']}])

    def test_legacy_logical_query_keeps_its_only_date_referent(self):
        query = logical_query('ledger', PERSONAL, year=2026, month=9, day=None)
        view = compact_user_context({'conversation': {'queries': [query], 'analysisWindow': None}})
        self.assertEqual(view['conversation']['queries'],
                         [logical_query('ledger', PERSONAL, year=2026, month=9)])
        self.assertNotIn('analysisWindow', view['conversation'])


if __name__ == '__main__':
    unittest.main()
