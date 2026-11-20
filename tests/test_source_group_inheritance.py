"""Offline replay of Luna's saved context and the confirmed failure shape.

The original invalid planner keys were not persisted. The unknown/duplicate
proposals below deliberately reconstruct that shape; they are not raw Luna
outputs. All correct amounts come from the real fictional ledger calculation.
"""

import json
import unittest
from copy import deepcopy
from datetime import date, datetime
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent.analysis_context import latest_context
from app.agent.book_agent import BookAgent
from app.agent.investigation import EvidenceInvestigator
from app.agent.planner import QueryPlanner
from scripts.evaluate_dialogues import DialogueLedger, DialogueMemory, OWNER


TODAY = date(2026, 11, 19)
SAVED_CONTEXT = json.loads(r'''{
  "targetMonth": "2027-01",
  "changes": [],
  "protectedCategories": [],
  "scope": [
    [
      "search_personal_orders",
      "\u4e0d\u9650",
      ""
    ]
  ],
  "activeAnalyses": [
    {
      "tool": "aggregate",
      "queryIds": [
        "ledger"
      ],
      "metric": "income",
      "aggregate": {
        "groupBy": [
          "source"
        ],
        "periods": [],
        "filters": {
          "includeCategories": [],
          "excludeCategories": [],
          "includeMembers": [],
          "excludeMembers": [],
          "includeSources": [],
          "excludeSources": [],
          "remarkContains": "\u661f\u6e2f\u6d77\u62a5",
          "excludeRemarks": []
        },
        "limit": 100
      },
      "changes": [],
      "protectedCategories": []
    },
    {
      "tool": "recurring",
      "queryIds": [
        "ledger"
      ],
      "metric": "income",
      "changes": [],
      "protectedCategories": []
    },
    {
      "tool": "forecast",
      "queryIds": [
        "ledger"
      ],
      "metric": "income",
      "targetMonth": "2027-01",
      "changes": [],
      "protectedCategories": []
    },
    {
      "tool": "data_quality",
      "queryIds": [],
      "metric": "income",
      "changes": [],
      "protectedCategories": []
    }
  ],
  "activeHistoryWindow": null,
  "conversation": {
    "objective": "\u6839\u636e\u4e2a\u4eba\u8d26\u672c\u4f30\u7b972027\u5e741\u6708\u6536\u5165\uff0c\u5e76\u5c06\u201c\u661f\u6e2f\u6d77\u62a5\u201d\u7684\u521d\u6392\u3001\u8c03\u7248\u548c\u5b8c\u7a3f\u8bb0\u5f55\u89c6\u4e3a\u540c\u4e00\u9879\u76ee\u3002",
    "analyses": [
      {
        "tool": "aggregate",
        "queryIds": [
          "ledger"
        ],
        "metric": "income",
        "aggregate": {
          "groupBy": [
            "source"
          ],
          "periods": [],
          "filters": {
            "includeCategories": [],
            "excludeCategories": [],
            "includeMembers": [],
            "excludeMembers": [],
            "includeSources": [],
            "excludeSources": [],
            "remarkContains": "\u661f\u6e2f\u6d77\u62a5",
            "excludeRemarks": []
          },
          "limit": 100
        },
        "changes": [],
        "protectedCategories": []
      },
      {
        "tool": "sources",
        "queryIds": [
          "ledger"
        ],
        "metric": "income",
        "changes": [],
        "protectedCategories": []
      },
      {
        "tool": "recurring",
        "queryIds": [
          "ledger"
        ],
        "metric": "income",
        "changes": [],
        "protectedCategories": []
      },
      {
        "tool": "forecast",
        "queryIds": [
          "ledger"
        ],
        "metric": "income",
        "targetMonth": "2027-01",
        "changes": [],
        "protectedCategories": []
      },
      {
        "tool": "data_quality",
        "queryIds": [],
        "metric": "income",
        "changes": [],
        "protectedCategories": []
      }
    ],
    "queries": [
      {
        "id": "ledger",
        "toolName": "search_personal_orders",
        "args": {
          "year": null,
          "month": null,
          "day": null,
          "cost_type": "\u4e0d\u9650",
          "remark": ""
        }
      }
    ],
    "analysisWindow": null,
    "output": {
      "table": false,
      "chart": "none",
      "metric": "income",
      "export": false
    },
    "sourceGroups": [
      {
        "label": "\u661f\u6e2f\u6d77\u62a5",
        "sourceKeys": [
          "src_45b9de927f6fcb99e99da92b",
          "src_cdc8bad5b7e959701e99501c",
          "src_e858bdefcc25ab8eae770d7a"
        ]
      }
    ]
  }
}''')
SAVED_FOLLOWUP = json.loads(r'''{
  "understanding": {
    "objective": "\u5c06\u521a\u624d\u5bf92027\u5e741\u6708\u4e2a\u4eba\u8d26\u672c\u6536\u5165\u7684\u9884\u6d4b\u6309\u6536\u5165\u9879\u76ee\u62c6\u5206\uff0c\u5217\u6210\u8868\u5e76\u7ed9\u51fa\u5408\u8ba1\u3002",
    "referents": [
      "\u201c\u521a\u624d\u9884\u6d4b\u7684\u90a3\u4e2a\u6570\u201d\u6307\u5386\u53f2\u6a21\u5f0f\u5ef6\u7eed\u4e0b2027\u5e741\u6708\u6536\u5165\u57fa\u51c63,700\u5143\u3002",
      "\u201c\u661f\u6e2f\u6d77\u62a5\u201d\u7684\u521d\u6392\u3001\u8c03\u7248\u548c\u5b8c\u7a3f\u6309\u540c\u4e00\u9879\u76ee\u5f52\u5e76\u3002",
      "\u6cbf\u7528\u4e2a\u4eba\u7248\u8d26\u672c\u53ca\u4e0a\u4e00\u8f6e\u5168\u8d26\u672c\u8303\u56f4\u3002"
    ],
    "requestedAnswers": [
      "\u6309\u6536\u5165\u9879\u76ee\u62c6\u52062027\u5e741\u6708\u9884\u6d4b\u91d1\u989d\u5e76\u4ee5\u8868\u683c\u5448\u73b0\u3002",
      "\u7ed9\u51fa\u9879\u76ee\u91d1\u989d\u5408\u8ba1\u3002"
    ],
    "evidenceNeeds": [
      "forecast",
      "sources",
      "aggregate"
    ],
    "unknowns": [],
    "conversationRelation": "follow_up"
  },
  "output": {
    "table": true,
    "chart": "none",
    "metric": "income",
    "export": false
  },
  "sourceGroups": [],
  "resetAnalysisContext": false,
  "intent": "analyze_orders",
  "analysisType": "financial_analysis",
  "needsTools": true,
  "analyses": [
    {
      "tool": "sources",
      "queryIds": [
        "ledger"
      ],
      "metric": "income",
      "aggregate": null,
      "targetMonth": null,
      "changes": [],
      "budgetAmount": null,
      "startingBalance": null,
      "targetAmount": null,
      "months": null,
      "monthsDelta": null,
      "monthlyImprovement": null,
      "purchaseAmount": null,
      "reserveAmount": null,
      "protectedCategories": []
    },
    {
      "tool": "forecast",
      "queryIds": [
        "ledger"
      ],
      "metric": "income",
      "aggregate": null,
      "targetMonth": "2027-01",
      "changes": [],
      "budgetAmount": null,
      "startingBalance": null,
      "targetAmount": null,
      "months": null,
      "monthsDelta": null,
      "monthlyImprovement": null,
      "purchaseAmount": null,
      "reserveAmount": null,
      "protectedCategories": []
    },
    {
      "tool": "aggregate",
      "queryIds": [],
      "metric": "income",
      "aggregate": {
        "groupBy": [],
        "periods": [],
        "filters": {
          "includeCategories": [],
          "excludeCategories": [],
          "includeMembers": [],
          "excludeMembers": [],
          "includeSources": [],
          "excludeSources": [],
          "remarkContains": "",
          "excludeRemarks": [],
          "minAmount": null,
          "maxAmount": null
        },
        "limit": 50
      },
      "targetMonth": null,
      "changes": [],
      "budgetAmount": null,
      "startingBalance": null,
      "targetAmount": null,
      "months": null,
      "monthsDelta": null,
      "monthlyImprovement": null,
      "purchaseAmount": null,
      "reserveAmount": null,
      "protectedCategories": []
    }
  ],
  "analysisWindow": null,
  "finalInstruction": "\u6309\u9879\u76ee\u5217\u51fa2027\u5e741\u6708\u6536\u5165\u9884\u6d4b\u91d1\u989d\u5e76\u6c42\u5408\u8ba1\uff1b\u5c06\u661f\u6e2f\u6d77\u62a5\u521d\u6392\u3001\u8c03\u7248\u3001\u5b8c\u7a3f\u5408\u5e76\u4e3a\u540c\u4e00\u9879\u76ee\u3002\u660e\u786e\u8bf4\u660e\u9884\u6d4b\u57fa\u51c6\u5408\u8ba1\u5e94\u4e0e\u4e0a\u4e00\u8f6e3,700\u5143\u4e00\u81f4\uff0c\u5982\u6765\u6e90\u62c6\u5206\u6570\u636e\u4e0d\u8db3\u4ee5\u91cd\u73b0\uff0c\u5219\u6307\u51fa\u5dee\u5f02\u53ca\u539f\u56e0\uff0c\u4e0d\u8981\u628a\u5386\u53f2\u9879\u76ee\u5165\u8d26\u989d\u76f4\u63a5\u5f53\u4f5c\u9884\u6d4b\u989d\u3002",
  "followUpOfPreviousQuestion": true
}''')
FOLLOWUP_QUESTION = '把刚才预测的那个数按每个项目拆出来，做个表，合计也要有。'


def replay_public_key(kind, value):
    # Replaying persisted public source keys needs the same public member
    # pseudonym across processes. No deployment secret is accessed or patched.
    if kind == 'member' and value == OWNER:
        return 'member_64bd40636a6a8c471985afba'
    return kind + '_' + sha256((kind + '\0' + value).encode()).hexdigest()[:24]


def followup_draft(groups):
    draft = deepcopy(SAVED_FOLLOWUP)
    # Date expansion is an execution detail. Recreate the logical planner input
    # from the query actually persisted alongside the first-turn context.
    draft['toolCalls'] = deepcopy(SAVED_CONTEXT['conversation']['queries'])
    draft['sourceGroups'] = deepcopy(groups)
    return draft


class SourceGroupInheritanceTests(unittest.IsolatedAsyncioTestCase):
    async def replay(self, draft, question=FOLLOWUP_QUESTION, book_mode='个人版'):
        planner = QueryPlanner.__new__(QueryPlanner)
        planner.chain = SimpleNamespace(ainvoke=AsyncMock(
            return_value=SimpleNamespace(content=json.dumps(draft, ensure_ascii=False))))
        previous = latest_context([{'role': 'assistant', 'metadata': {
            'analysisContext': deepcopy(SAVED_CONTEXT)}}])
        with patch('app.agent.planner.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 19)
            plan = await planner.create_plan(question, '', book_mode, previous)
        agent = BookAgent.__new__(BookAgent)
        agent.ledger_client = DialogueLedger(TODAY)
        agent.memory = DialogueMemory()
        agent.investigator = EvidenceInvestigator()
        # Exercise the real investigation fallback without making an API call.
        agent.investigator._invoke = AsyncMock(side_effect=RuntimeError('fictional investigator unavailable'))
        with (patch('app.agent.book_agent.datetime', wraps=datetime) as clock,
              patch('app.tools.order_search._opaque_key', side_effect=replay_public_key)):
            clock.now.return_value = datetime(2026, 11, 19)
            evidence = json.loads(await agent._execute_tool_calls(
                book_mode, 'synthetic-household' if book_mode == '家庭版' else '',
                plan, question=question))
        observed = {key for result in evidence['analysisResults'] if result['tool'] == 'sources'
                    for row in result['evidence'].get('sources', [])
                    for key in row.get('observedSourceKeys', [])}
        self.assertTrue(set(SAVED_CONTEXT['conversation']['sourceGroups'][0]['sourceKeys']) <= observed,
                        'The saved fictional member identity must match the current authorized catalog')
        await agent._save_assistant_reply('fictional', 'fictional', '已计算', plan, evidence=evidence)
        stored = latest_context(json.loads(json.dumps(agent.memory.messages, ensure_ascii=False)))
        return plan, evidence, stored

    async def test_invalid_nonempty_proposal_cannot_erase_confirmed_groups_when_investigation_is_unavailable(self):
        confirmed = SAVED_CONTEXT['conversation']['sourceGroups'][0]
        keys = confirmed['sourceKeys']
        proposals = [
            [{'label': confirmed['label'], 'sourceKeys': ['unobserved-reconstructed-key']}],
            [{'label': confirmed['label'], 'sourceKeys': [keys[0], keys[0]]}],
            [{'label': confirmed['label'], 'sourceKeys': [keys[0], keys[1]]},
             {'label': confirmed['label'], 'sourceKeys': [keys[1], keys[2]]}],
        ]
        for groups in proposals:
            with self.subTest(proposals=groups):
                plan, evidence, stored = await self.replay(followup_draft(groups))
                self.assertEqual(plan._investigation['status'], 'unavailable')
                forecast, = [row['evidence'] for row in evidence['analysisResults'] if row['tool'] == 'forecast']
                self.assertEqual(forecast['estimate'], 3700)
                self.assertEqual(forecast['baselineMonths'], ['2026-08', '2026-09', '2026-10'])
                project, = [row for row in forecast['sourceForecasts'] if row['source'] == '星港海报']
                self.assertEqual(set(project['remarks']), {'星港海报初排', '星港海报调版', '星港海报完稿'})
                self.assertEqual(project['estimate'], 500)
                self.assertEqual(stored['scope'], SAVED_CONTEXT['scope'])
                saved_group, = stored['conversation']['sourceGroups']
                self.assertEqual(set(saved_group['sourceKeys']), set(keys))

    async def test_independent_reset_and_changed_ledger_or_filter_do_not_restore_previous_groups(self):
        cases = [
            ('独立问题，预测2027年1月收入，不沿用原来的项目分组。',
             {'followUpOfPreviousQuestion': False}, '个人版'),
            ('重置刚才的分组，重新预测2027年1月收入。',
             {'resetAnalysisContext': True}, '个人版'),
            ('改看家庭账本，预测2027年1月收入。', {'toolCalls': [
                {'id': 'ledger', 'toolName': 'search_family_orders'}]}, '家庭版'),
            ('只看备注包含星港海报的账单，预测2027年1月收入。', {'toolCalls': [
                {'id': 'ledger', 'toolName': 'search_personal_orders', 'args': {'remark': '星港海报'}}]}, '个人版'),
        ]
        for question, updates, mode in cases:
            with self.subTest(question=question):
                draft = followup_draft([])
                draft.update(updates)
                plan, _, stored = await self.replay(draft, question, mode)
                self.assertEqual(plan.sourceGroups, [])
                self.assertEqual(stored['conversation']['sourceGroups'], [])

    async def test_explicit_ungrouping_does_not_restore_the_previous_project(self):
        for question in (
                '刚才的目标月不变，但不再把星港海报的初排、调版和完稿合为同一个项目，各自作为独立收入来源重新算。',
                '这次星港海报的初排、调版和完稿各自作为独立收入来源重新算。',
                '取消原先的星港海报归并，初排、调版和完稿各算各的。'):
            with self.subTest(question=question):
                plan, evidence, stored = await self.replay(followup_draft([]), question)
                self.assertEqual(plan.sourceGroups, [])
                self.assertEqual(stored['conversation']['sourceGroups'], [])
                forecast, = [row['evidence'] for row in evidence['analysisResults'] if row['tool'] == 'forecast']
                self.assertEqual(forecast['estimate'], 3200)

    async def test_regrouping_expense_categories_keeps_confirmed_income_source_groups(self):
        question = '支出按分类重新分组，同样预测目标月收入，星港海报保持原样。'
        draft = followup_draft([])
        aggregate = next(task for task in draft['analyses'] if task['tool'] == 'aggregate')
        aggregate['metric'] = 'expense'
        aggregate['aggregate']['groupBy'] = ['category']
        plan, _, stored = await self.replay(draft, question)
        expected = SAVED_CONTEXT['conversation']['sourceGroups']
        self.assertEqual([group.model_dump() for group in plan.sourceGroups], expected)
        self.assertEqual(stored['conversation']['sourceGroups'], expected)

    async def test_explicit_partial_regrouping_keeps_the_new_verified_structure(self):
        group = SAVED_CONTEXT['conversation']['sourceGroups'][0]
        regrouped = {'label': group['label'], 'sourceKeys': group['sourceKeys'][:2]}
        question = '目标月沿用刚才的，但这次把星港海报调版单列，只把初排和完稿合为同一项目。'
        plan, evidence, stored = await self.replay(followup_draft([regrouped]), question)
        saved_group, = stored['conversation']['sourceGroups']
        self.assertEqual(set(saved_group['sourceKeys']), set(regrouped['sourceKeys']))
        self.assertEqual(len(plan.sourceGroups), 1)
        forecast, = [row['evidence'] for row in evidence['analysisResults'] if row['tool'] == 'forecast']
        self.assertEqual({row['source'] for row in forecast['sourceForecasts'] if not row['excluded']},
                         {'河畔编辑部月结', '星港海报', '星港海报调版'})


if __name__ == '__main__':
    unittest.main()
