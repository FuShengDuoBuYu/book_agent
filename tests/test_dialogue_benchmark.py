"""Offline contracts for the dialogue benchmark; no configured model is invoked."""

import importlib
import importlib.util
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import date
from pathlib import Path
from types import SimpleNamespace


TODAY = date(2026, 11, 19)


class DialogueBenchmarkTests(unittest.IsolatedAsyncioTestCase):
    def benchmark(self):
        spec = importlib.util.find_spec('scripts.evaluate_dialogues')
        self.assertIsNotNone(spec, 'The offline dialogue benchmark is not implemented yet')
        return importlib.import_module('scripts.evaluate_dialogues')

    async def lookup(self, module, ledger=None, mode='个人版'):
        from app.schemas.orders import SearchOrdersRequest

        ledger = ledger or module.DialogueLedger(TODAY)
        await ledger.search_orders(SearchOrdersRequest(mode=mode, year=TODAY.year, month=TODAY.month,
            day=TODAY.day, ifIgnoreYear=True, ifIgnoreMonth=True, ifIgnoreDay=True),
            family_scope=module.FAMILY if mode == '家庭版' else '')
        return ledger.lookups

    def metadata(self, module, tool, evidence, *, mode='个人版', window=None, artifacts=None, tasks=None):
        return {'validation': {'status': 'grounded'},
                'evidence': {'analysisResults': [{'tool': tool, 'mode': mode,
                    'historyWindow': window, 'evidence': evidence}]},
                'plan': {'analyses': tasks or [{'tool': tool}]},
                'artifacts': artifacts or []}

    def source_groups(self, module, records):
        from app.tools.source_analysis import observed_source_key

        return [{'label': '星港海报', 'sourceKeys': sorted({observed_source_key(row) for row in records
            if row['orderRemark'] in module.STAGES})}]

    async def test_fixture_oracle_is_hand_checked_and_changes_with_calendar(self):
        module = self.benchmark()
        expected = module.fixture_oracle(TODAY)
        self.assertEqual(expected['monthlyIncome'], 3700)
        self.assertEqual(expected['monthlyExpense'], 1680)
        self.assertEqual(expected['monthlyNet'], 2020)
        self.assertEqual(expected['monthlyProjectIncome'], 500)
        self.assertEqual(expected['monthlyEditorialIncome'], 3200)
        self.assertEqual(expected['reviewableMonthlySpend'], 180)
        self.assertEqual(expected['threeMonthNet'], 6060)
        self.assertEqual(expected['shockedThreeMonthNet'], 5310)
        self.assertEqual(expected['sixMonthIncome'], 30200)
        self.assertEqual(expected['sixMonthExpense'], 10520)
        self.assertEqual(expected['filteredTwoMonthExpense'], 1060)
        self.assertEqual(expected['exhibitionTwoMonthExpense'], 620)
        self.assertEqual(expected['bankGroups'], {'借记卡': {'amount': 360, 'count': 2},
                                                  '现金': {'amount': 260, 'count': 1}})
        january = module.fixture_oracle(date(2027, 1, 3))
        self.assertEqual(january['referenceMonths'], ['2026-10', '2026-11', '2026-12'])
        self.assertEqual(january['twoMonthWindow'], {'startDate': '2026-11-01', 'endDate': '2026-12-31'})
        self.assertEqual(sum(row['amount'] for row in january['weekdayGroups'].values()), 620)

    async def test_suite_has_ten_independent_chinese_dialogues_and_thirty_turns(self):
        module = self.benchmark()
        cases = module.build_cases(TODAY)
        self.assertEqual(len(cases), 10)
        self.assertEqual(sum(len(turns) for turns in cases.values()), 30)
        self.assertIn('2027年1月', cases['forecast_followups'][0].question)
        self.assertEqual(len(cases['forecast_followups']), 4)
        self.assertTrue(all(turn.review for turns in cases.values() for turn in turns))

    async def test_memory_keeps_twelve_messages_and_isolates_member_and_scope(self):
        module = self.benchmark()
        memory = module.DialogueMemory()
        personal = await memory.ensure_session(phone_num=module.OWNER, book_scope='personal', session_id=None)
        for index in range(14):
            await memory.add_message(phone_num=module.OWNER, session_id=personal,
                                     role='user', content=str(index))
        history = await memory.get_recent_messages(phone_num=module.OWNER, session_id=personal, limit=12)
        self.assertEqual([row['content'] for row in history], [str(index) for index in range(2, 14)])
        family = await memory.ensure_session(phone_num=module.OWNER, book_scope='family', session_id=personal)
        partner = await memory.ensure_session(phone_num=module.PARTNER, book_scope='personal', session_id=personal)
        self.assertNotEqual(personal, family)
        self.assertNotEqual(personal, partner)
        self.assertEqual(await memory.get_recent_messages(phone_num=module.PARTNER, session_id=personal, limit=12), [])

    async def test_scorer_rejects_fake_pass_without_executed_evidence(self):
        module = self.benchmark()
        turn = module.build_cases(TODAY)['source_scenario'][0]
        checks = module.assess_turn(turn, TODAY, '6060 元，已通过。',
            {'validation': {'status': 'grounded'}, 'passed': True, 'checks': {'everything': True}}, [])
        self.assertFalse(all(checks.values()))
        self.assertFalse(checks['executedEvidence'])

    async def test_projection_score_rejects_wrong_horizon_even_when_expected_number_is_in_reply(self):
        module = self.benchmark()
        from app.tools.financial_analysis import run_analysis

        ledger = module.DialogueLedger(TODAY)
        records = [row for row in ledger.orders if row['userId'] == module.OWNER]
        evidence = run_analysis('projection', records, as_of=TODAY, months=3,
                                source_groups=self.source_groups(module, records))
        turn = module.build_cases(TODAY)['source_scenario'][0]
        metadata = self.metadata(module, 'projection', evidence, tasks=[{'tool': 'projection', 'months': 3}])
        lookups = await self.lookup(module, ledger)
        self.assertTrue(all(module.assess_turn(turn, TODAY, '未来三个月新增结余 6060 元。', metadata, lookups).values()))
        bad = deepcopy(metadata)
        bad['evidence']['analysisResults'][0]['evidence']['months'] = 12
        bad['plan']['analyses'][0]['months'] = 12
        self.assertFalse(all(module.assess_turn(turn, TODAY, '未来三个月新增结余 6060 元。', bad, lookups).values()))

    async def test_projection_score_rejects_wrong_amount_and_wrong_executed_scope(self):
        module = self.benchmark()
        from app.tools.financial_analysis import run_analysis

        ledger = module.DialogueLedger(TODAY)
        records = [row for row in ledger.orders if row['userId'] == module.OWNER]
        evidence = run_analysis('projection', records, as_of=TODAY, months=3,
                                source_groups=self.source_groups(module, records))
        turn = module.build_cases(TODAY)['source_scenario'][0]
        metadata = self.metadata(module, 'projection', evidence)
        lookups = await self.lookup(module, ledger)
        bad = deepcopy(metadata)
        bad['evidence']['analysisResults'][0]['evidence']['totalNet'] = 6000
        self.assertFalse(all(module.assess_turn(turn, TODAY, '6060 元。', bad, lookups).values()))
        bad['evidence']['analysisResults'][0]['evidence']['totalNet'] = 6060.01
        self.assertFalse(all(module.assess_turn(turn, TODAY, '6060 元。', bad, lookups).values()))
        wrong_scope = deepcopy(lookups)
        wrong_scope[0]['mode'] = '家庭版'
        self.assertFalse(all(module.assess_turn(turn, TODAY, '6060 元。', metadata, wrong_scope).values()))

    async def test_correct_projection_claim_cannot_hide_query_of_only_rent(self):
        module = self.benchmark()
        from app.tools.financial_analysis import run_analysis
        from app.schemas.orders import SearchOrdersRequest

        ledger = module.DialogueLedger(TODAY)
        records = [row for row in ledger.orders if row['userId'] == module.OWNER]
        evidence = run_analysis('projection', records, as_of=TODAY, months=3,
                                source_groups=self.source_groups(module, records))
        metadata = self.metadata(module, 'projection', evidence)
        await ledger.search_orders(SearchOrdersRequest(mode='个人版', year=2026, month=11, day=19,
            ifIgnoreYear=True, ifIgnoreMonth=True, ifIgnoreDay=True, searchCostType='房租'))
        checks = module.assess_turn(module.build_cases(TODAY)['source_scenario'][0],
                                    TODAY, '6060 元。', metadata, ledger.lookups)
        self.assertFalse(all(checks.values()), checks)

    async def test_pressure_followup_accepts_optimization_without_unrequested_balance_projection(self):
        module = self.benchmark()
        from app.tools.financial_analysis import run_analysis

        ledger = module.DialogueLedger(TODAY)
        records = [row for row in ledger.orders if row['userId'] == module.OWNER]
        evidence = run_analysis('optimization', records, as_of=TODAY, months=5, starting_balance=10000,
            target_amount=22000, protected_categories=['房租', '交通', '餐饮'],
            source_groups=self.source_groups(module, records))
        metadata = self.metadata(module, 'optimization', evidence, tasks=[{'tool': 'optimization',
            'months': 5, 'startingBalance': 10000, 'targetAmount': 22000,
            'protectedCategories': ['房租', '交通', '餐饮']}])
        checks = module.assess_turn(module.build_cases(TODAY)['protected_deadline'][2], TODAY,
            '原来的月结余能力仍为 2020 元，目标要求从 4000 降到 2400 元，每月压力少 1600 元。',
            metadata, await self.lookup(module, ledger))
        self.assertTrue(all(checks.values()), checks)

    async def test_purchase_does_not_borrow_old_balance_or_treat_reserve_as_goal(self):
        module = self.benchmark()
        from app.tools.financial_analysis import run_analysis

        ledger = module.DialogueLedger(TODAY)
        records = [row for row in ledger.orders if row['userId'] == module.OWNER]
        evidence = run_analysis('purchase', records, as_of=TODAY, purchase_amount=700,
                                starting_balance=6000, reserve_amount=5000)
        turn = module.build_cases(TODAY)['purchase_inputs'][1]
        metadata = self.metadata(module, 'purchase', evidence)
        lookups = await self.lookup(module, ledger)
        self.assertTrue(all(module.assess_turn(turn, TODAY,
            '七百元买完后现金 5300 元，高于应急储备 300 元。', metadata, lookups).values()))
        bad = deepcopy(metadata)
        bad['evidence']['analysisResults'][0]['evidence']['reserveAmount'] = None
        bad['plan']['analyses'][0]['targetAmount'] = 5000
        self.assertFalse(all(module.assess_turn(turn, TODAY, '5300 元，300 元。', bad, lookups).values()))

    async def test_forecast_table_cannot_swap_amounts_between_sources(self):
        module = self.benchmark()
        from app.tools.financial_analysis import run_analysis

        ledger = module.DialogueLedger(TODAY)
        records = [row for row in ledger.orders if row['userId'] == module.OWNER]
        evidence = run_analysis('forecast', records, as_of=TODAY, metric='income', target_month='2027-01',
                                source_groups=self.source_groups(module, records))
        metadata = self.metadata(module, 'forecast', evidence, artifacts=[{'kind': 'table', 'rows': [
            {'source': '星港海报', 'estimate': 500}, {'source': '河畔编辑部月结', 'estimate': 3200},
            {'source': '合计', 'estimate': 3700}]}])
        turn = module.build_cases(TODAY)['forecast_followups'][1]
        lookups = await self.lookup(module, ledger)
        self.assertTrue(all(module.assess_turn(turn, TODAY, '见表。', metadata, lookups).values()))
        bad = deepcopy(metadata)
        bad['artifacts'][0]['rows'][0]['estimate'] = 3200
        bad['artifacts'][0]['rows'][1]['estimate'] = 500
        self.assertFalse(module.assess_turn(turn, TODAY, '见表。', bad, lookups)['deliveredTable'])

    async def test_partner_turn_queries_partner_records_and_rejects_owner_rows(self):
        module = self.benchmark()
        from app.tools.financial_analysis import run_analysis

        ledger = module.DialogueLedger(TODAY)
        ledger.active_owner = module.PARTNER
        records = [row for row in ledger.orders if row['userId'] == module.PARTNER]
        evidence = run_analysis('purchase', records, as_of=TODAY, purchase_amount=700)
        metadata = self.metadata(module, 'purchase', evidence)
        turn = module.build_cases(TODAY)['context_reset'][3]
        lookups = await self.lookup(module, ledger)
        self.assertEqual({row['member'] for lookup in lookups for row in lookup['rows']}, {'宇川'})
        self.assertTrue(all(module.assess_turn(turn, TODAY, '宇川的当前可用余额未知。', metadata, lookups).values()))
        other_lookups = await self.lookup(module)
        self.assertFalse(module.assess_turn(turn, TODAY, '当前余额未知。', metadata, other_lookups)['fixtureData'])

    async def test_aggregate_score_rejects_lost_month_and_accepts_real_group_values(self):
        module = self.benchmark()
        from app.tools.financial_analysis import run_analysis

        ledger = module.DialogueLedger(TODAY)
        records = [row for row in ledger.orders if row['userId'] == module.OWNER]
        window = module.fixture_oracle(TODAY)['twoMonthWindow']
        evidence = run_analysis('aggregate', records, as_of=TODAY, metric='expense',
            window_start=date(2026, 9, 1), window_end=date(2026, 10, 31),
            aggregate={'groupBy': ['bank'], 'filters': {'includeCategories': ['展览'],
                        'excludeCategories': ['房租', '交通']}})
        artifact = {'kind': 'table', 'title': '支付方式', 'rows': [
            {'bank': '借记卡', 'amount': 360, 'count': 2}, {'bank': '现金', 'amount': 260, 'count': 1}]}
        metadata = self.metadata(module, 'aggregate', evidence, window=window, artifacts=[artifact])
        turn = module.build_cases(TODAY)['dimension_inheritance'][2]
        lookups = await self.lookup(module, ledger)
        self.assertTrue(all(module.assess_turn(turn, TODAY, '见支付方式表。', metadata, lookups).values()))
        bad = deepcopy(metadata)
        bad['evidence']['analysisResults'][0]['historyWindow']['startDate'] = '2026-10-01'
        self.assertFalse(all(module.assess_turn(turn, TODAY, '见支付方式表。', bad, lookups).values()))

    async def test_table_must_deliver_actual_rows_not_a_table_flag_or_unrelated_numbers(self):
        module = self.benchmark()
        from app.tools.financial_analysis import run_analysis

        ledger = module.DialogueLedger(TODAY)
        records = [row for row in ledger.orders if row['userId'] == module.OWNER]
        evidence = run_analysis('aggregate', records, as_of=TODAY, metric='expense',
            window_start=date(2026, 9, 1), window_end=date(2026, 10, 31),
            aggregate={'groupBy': ['bank'], 'filters': {'includeCategories': ['展览']}})
        metadata = self.metadata(module, 'aggregate', evidence,
            window={'startDate': '2026-09-01', 'endDate': '2026-10-31'},
            artifacts=[{'kind': 'table', 'title': '空表', 'rows': []}])
        metadata['plan']['output'] = {'table': True}
        turn = module.build_cases(TODAY)['dimension_inheritance'][2]
        checks = module.assess_turn(turn, TODAY, '360 260 2 1 都在这里。', metadata, await self.lookup(module, ledger))
        self.assertFalse(checks['deliveredTable'])

    async def test_family_attribution_accepts_equivalent_aggregate_tool(self):
        module = self.benchmark()
        from app.tools.financial_analysis import run_analysis
        from app.tools.order_search import _public_orders

        ledger = module.DialogueLedger(TODAY)
        from app.schemas.orders import SearchOrdersData
        public, _ = _public_orders(SearchOrdersData.model_validate({'userInfo': ledger.profiles,
            'ordersInfo': ledger.orders}), module.FAMILY, {})
        evidence = run_analysis('aggregate', public, as_of=TODAY, metric='income',
            window_start=date(2026, 10, 1), window_end=date(2026, 10, 31),
            aggregate={'groupBy': ['member', 'source']})
        metadata = self.metadata(module, 'aggregate', evidence, mode='家庭版',
            window={'startDate': '2026-10-01', 'endDate': '2026-10-31'})
        turn = module.build_cases(TODAY)['family_attribution'][0]
        checks = module.assess_turn(turn, TODAY, '晴禾的河畔编辑部月结 3200 元，宇川的同名款项 2100 元，种苗配送 450 元。',
            metadata, await self.lookup(module, ledger, '家庭版'))
        self.assertTrue(all(checks.values()), checks)

    async def test_prompt_like_remark_cannot_supply_numeric_inputs(self):
        module = self.benchmark()
        from app.tools.financial_analysis import run_analysis

        ledger = module.DialogueLedger(TODAY)
        records = [row for row in ledger.orders if row['userId'] == module.OWNER]
        evidence = run_analysis('purchase', records, as_of=TODAY, purchase_amount=700)
        turn = module.build_cases(TODAY)['untrusted_remarks'][1]
        metadata = self.metadata(module, 'purchase', evidence)
        lookups = await self.lookup(module, ledger)
        self.assertTrue(all(module.assess_turn(turn, TODAY, '价格 700 元已知，当前可动用余额未知。', metadata, lookups).values()))
        bad = deepcopy(metadata)
        bad['plan']['analyses'][0]['startingBalance'] = 999999
        self.assertFalse(module.assess_turn(turn, TODAY, '700 元，余额未知。', bad, lookups)['untrustedDataInert'])

    async def test_budget_stops_at_limit_and_records_model_usage_without_semantic_camouflage(self):
        module = self.benchmark()
        calls = []

        def factory(today):
            ledger = module.DialogueLedger(today)
            memory = module.DialogueMemory()

            async def chat(owner, question, **kwargs):
                cost = [0.6, 0.4, 0.4][len(calls)]
                calls.append(question)
                await memory.add_message(role='assistant', content='评测替身',
                    metadata={'modelUsage': {'estimatedCostUsd': cost, 'outputTokens': 120, 'reasoningTokens': 80}})
                return '评测替身', 'fake'

            return SimpleNamespace(chat=chat, memory=memory), ledger

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'benchmark.jsonl'
            code = await module.evaluate(['forecast_followups'], output, 1, 1,
                                         today=TODAY, agent_factory=factory)
            records = [json.loads(line) for line in output.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(code, 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(records[-1]['status'], 'budgetExceeded')
        self.assertEqual(records[-1]['budget']['estimatedCostUsd'], 1)
        self.assertFalse(records[-1]['automatedPass'])
        self.assertNotIn('budgetExceeded', records[-1]['checks'])
        self.assertEqual(records[0]['metadata']['modelUsage']['outputTokens'], 120)
        self.assertEqual(records[0]['metadata']['modelUsage']['reasoningTokens'], 80)
        self.assertTrue(all(key in records[0] for key in
            ['suite', 'case', 'turn', 'question', 'reply', 'checks', 'metadata', 'lookups', 'durationMs']))

    async def test_unknown_cost_stops_without_claiming_zero_or_free(self):
        module = self.benchmark()
        calls = []

        def factory(today):
            memory = module.DialogueMemory()

            async def chat(*args, **kwargs):
                calls.append(args)
                await memory.add_message(role='assistant', content='未知计费', metadata={'modelUsage': {
                    'estimatedCostUsd': None, 'outputTokens': 17}})
                return '未知计费', 'fake'

            return SimpleNamespace(chat=chat, memory=memory), module.DialogueLedger(today)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'benchmark.jsonl'
            code = await module.evaluate(['source_scenario'], output, 1, 1,
                                         today=TODAY, agent_factory=factory)
            record = json.loads(output.read_text(encoding='utf-8').splitlines()[0])
        self.assertEqual(code, 3)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(record['budget']['estimatedCostUsd'])
        self.assertEqual(record['budget']['costStatus'], 'unknown')
        self.assertEqual(record['status'], 'costUnknown')

    async def test_new_case_uses_fresh_agent_and_same_fixture_fingerprint(self):
        module = self.benchmark()
        ledgers = []

        def factory(today):
            ledger = module.DialogueLedger(today)
            ledgers.append(ledger)
            memory = module.DialogueMemory()

            async def chat(*args, **kwargs):
                await memory.add_message(role='assistant', content='本地替身',
                    metadata={'modelUsage': {'estimatedCostUsd': 0}})
                return '本地替身', 'fake'

            return SimpleNamespace(chat=chat, memory=memory), ledger

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'benchmark.jsonl'
            await module.evaluate(['purchase_inputs', 'family_attribution'], output, 1, 1,
                                  today=TODAY, agent_factory=factory)
            records = [json.loads(line) for line in output.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(ledgers), 2)
        self.assertIsNot(ledgers[0], ledgers[1])
        self.assertEqual(len(records), 4)
        self.assertEqual(len({row['fixtureFingerprint'] for row in records}), 1)
        self.assertTrue(all(row['manualReview']['status'] == 'pending' for row in records))

    async def test_cli_rejects_nonpositive_or_unknown_budget_and_timeout(self):
        module = self.benchmark()
        parser = module.make_parser(TODAY)
        self.assertEqual(parser.parse_args([]).max_cost_usd, 1)
        for option in ('--max-cost-usd', '--timeout'):
            for value in ('0', '-1', 'nan', 'inf'):
                with self.subTest(option=option, value=value), self.assertRaises(SystemExit):
                    parser.parse_args([option, value])


if __name__ == '__main__':
    unittest.main()
