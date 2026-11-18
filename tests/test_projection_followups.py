import json
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent.analysis_context import latest_context
from app.agent.book_agent import BookAgent
from app.agent.evidence_pipeline import collect_analysis_results
from app.agent.plan_review import PlanReviewer
from app.agent.planner import QueryPlanner
from app.schemas.plan import AgentPlan


class ProjectionFollowups(unittest.IsolatedAsyncioTestCase):
    async def plan(self, text, draft, previous):
        planner = QueryPlanner.__new__(QueryPlanner)
        planner.chain = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps(draft))))
        with patch('app.agent.planner.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 19)
            return await planner.create_plan(text, '', '个人版', previous)

    async def save(self, plan):
        agent = BookAgent.__new__(BookAgent)
        agent.memory = AsyncMock()
        await agent._save_assistant_reply('synthetic', 'synthetic', '核算结果', plan)
        return latest_context(json.loads(json.dumps([agent.memory.add_message.await_args.kwargs])))

    async def test_named_shock_protected_expenses_and_extension_use_one_actual_projection(self):
        rows = [{'year': 2026, 'month': m, 'day': 4, 'money': value, 'costType': category, 'orderRemark': remark}
                for m in (8, 9, 10) for value, category, remark in
                [(5000, '工资', '工资'), (1600, '收入', '翻译稿费'), (-2600, '房租', '房租')]]
        first = await self.plan('未来三个月能存多少？', {'analyses': [{'tool': 'projection', 'months': 3}]}, {})
        previous = await self.save(first)
        turns = [
            ('翻译稿费可能会少一半，重新算一下。', {'analyses': [{'tool': 'scenario', 'metric': 'net'}],
                'understanding': {'objective': '将翻译稿费收入减少一半，重算累计结余'}}, 3),
            ('房租不动，再看看其余开支。', {'analyses': [{'tool': 'optimization', 'protectedCategories': ['房租']}],
                'understanding': {'requestedAnswers': ['哪些项目能调整', '重新测算累计结余']}}, 3),
            ('那把目标推迟两个月，压力能小多少？', {'analyses': [{'tool': 'projection', 'months': 5}]}, 5)]
        for question, proposal, expected_months in turns:
            plan = await self.plan(question, {**proposal, 'followUpOfPreviousQuestion': True}, previous)
            projections = [t for t in plan.analyses if t.tool == 'projection']
            self.assertEqual(len(projections), 1, question)
            task = projections[0]
            self.assertEqual((task.months, task.targetMonth), (expected_months, '2026-12'))
            self.assertEqual([(c.metric, c.source, c.percent) for c in task.changes], [('income', '翻译稿费', -50)])
            results = collect_analysis_results(plan, {'个人版': [(c, rows) for c in plan.toolCalls]}, date(2026, 11, 19))
            evidence = next(r['evidence'] for r in results if r['tool'] == 'projection')
            self.assertEqual((evidence['monthlyNet'], evidence['totalNet']), (3200, 3200 * expected_months))
            previous = await self.save(plan)
        self.assertEqual(previous['previousMonths'], 3)
        self.assertEqual(previous['protectedCategories'], ['房租'])

    async def test_review_must_not_widen_named_income_or_expense_to_every_source(self):
        for question, metric in [('绘图收入可能减少30%，重算', 'income'), ('培训支出将减半，重新算', 'expense')]:
            percent = -30 if metric == 'income' else -50
            model = AsyncMock()
            decision = {'adequate': True, 'issues': [], 'analysisWindow': None, 'answerInstruction': '',
                'analyses': [{'tool': 'scenario', 'changes': [{'metric': metric, 'percent': percent, 'source': ''}]}]}
            model.ainvoke.return_value = SimpleNamespace(content=json.dumps(decision), response_metadata={})
            with patch('app.agent.plan_review.get_structured_model', return_value=model):
                response, audit = await PlanReviewer().review(question, '', AgentPlan(analyses=[{'tool': 'scenario'}]),
                    date(2026, 11, 19), '个人版', {})
            self.assertIsNone(response)
            self.assertEqual(audit.get('error'), 'unsupported_changes')

    async def test_inconsistent_adequacy_requires_explicit_corrected_plan(self):
        original = AgentPlan(analyses=[{'tool': 'scenario'}])
        changed = {'adequate': True, 'issues': [], 'analysisWindow': None, 'answerInstruction': '',
            'analyses': [{'tool': 'projection', 'months': 3, 'changes': [
                {'metric': 'income', 'source': '稿费', 'percent': -50}]}]}
        model = AsyncMock()
        model.ainvoke.side_effect = [SimpleNamespace(content=json.dumps(v), response_metadata={})
            for v in [changed, {**changed, 'adequate': False, 'issues': ['缺少三个月累计结余和收入变化条件']}]]
        with patch('app.agent.plan_review.get_structured_model', return_value=model):
            response, audit = await PlanReviewer().review('未来三个月如果稿费减少一半，累计结余多少？', '',
                original, date(2026, 11, 19), '个人版', {})
        self.assertIsNotNone(response)
        self.assertFalse(response.adequate)
        self.assertEqual(model.ainvoke.await_count, 2)
        self.assertEqual(audit['status'], 'reviewed')

    async def test_default_driver_window_needs_two_complete_months_but_preserves_an_explicit_limit(self):
        draft = {'analyses': [{'tool': 'changes'}],
                 'analysisWindow': {'startDate': '2026-10-01', 'endDate': '2026-11-19'}}
        plan = await self.plan('我最近感觉花钱越来越多，哪里增加了？', draft, {})
        self.assertEqual(plan.analysisWindow.startDate, date(2026, 9, 1))
        for text, start, end in [('只看上个月', '2026-10-01', '2026-10-31'),
                                 ('只看昨天', '2026-11-18', '2026-11-18')]:
            plan = await self.plan(text, {**draft, 'analysisWindow': {'startDate': start, 'endDate': end}}, {})
            self.assertEqual(plan.analysisWindow.model_dump(mode='json'), {'startDate': start, 'endDate': end})

    async def test_explicit_future_outlook_gets_full_horizon_and_no_artificial_sample_limit(self):
        draft = {'analyses': [{'tool': 'trends'}, {'tool': 'forecast', 'metric': 'net'}],
                 'analysisWindow': {'startDate': '2026-10-01', 'endDate': '2026-11-19'}}
        plan = await self.plan('按现在的趋势，未来三个月收支会怎样？', draft, {})
        self.assertEqual([(t.months, t.targetMonth) for t in plan.analyses if t.tool == 'projection'], [(3, '2026-12')])
        self.assertLessEqual(min(c.args.month for c in plan.toolCalls), 8)
        limited = await self.plan('参考上个月的收支，未来三个月会怎样？', draft, {})
        self.assertEqual(limited.analysisWindow.startDate, date(2026, 10, 1))
        past = await self.plan('过去三个月收支怎样？', {'analyses': [{'tool': 'trends'}]}, {})
        self.assertFalse(any(t.tool == 'projection' for t in past.analyses))

    async def test_two_relative_months_correct_window_and_aggregate_periods(self):
        periods = [{'label': '上上月', 'startDate': '2026-10-01', 'endDate': '2026-10-31'},
                   {'label': '上月', 'startDate': '2026-11-01', 'endDate': '2026-11-30'}]
        for proposed in ([], periods):
            plan = await self.plan('对照上上个月和上个月，按月份和类别列出支出。',
                {'analyses': [{'tool': 'aggregate', 'metric': 'expense', 'aggregate': {'groupBy': ['month', 'category'], 'periods': proposed}}],
                 'analysisWindow': {'startDate': '2026-10-01', 'endDate': '2026-11-30'}}, {})
            self.assertEqual(plan.analysisWindow.model_dump(mode='json'), {'startDate': '2026-09-01', 'endDate': '2026-10-31'})
            self.assertEqual([(p.startDate.month, p.endDate.month) for p in plan.analyses[0].aggregate.periods], [(9,9),(10,10)])

    async def test_historical_month_count_does_not_invalidate_historical_review(self):
        task = {'tool': 'cashflow', 'months': 6}
        decision = {'adequate': True, 'analyses': [task], 'issues': [], 'analysisWindow': None, 'answerInstruction': ''}
        model = AsyncMock()
        model.ainvoke.return_value = SimpleNamespace(content=json.dumps(decision))
        with patch('app.agent.plan_review.get_structured_model', return_value=model):
            result, audit = await PlanReviewer().review('回顾过去半年账本', '', AgentPlan(analyses=[task]), date(2026,11,19), '个人版', {})
        self.assertIsNotNone(result, audit)

    async def test_failed_review_retry_keeps_known_execution_conflict_visible(self):
        model = AsyncMock()
        inconsistent = {'adequate': True, 'issues': [], 'analysisWindow': None, 'answerInstruction': '',
                        'analyses': [{'tool': 'projection', 'months': 3}]}
        model.ainvoke.side_effect = [SimpleNamespace(content=json.dumps(inconsistent)), TimeoutError()]
        with patch('app.agent.plan_review.get_structured_model', return_value=model):
            result, audit = await PlanReviewer().review('未来三个月的结余呢？', '', AgentPlan(analyses=[{'tool':'trends'}]),
                                                       date(2026,11,19), '个人版', {})
        self.assertIsNone(result)
        self.assertEqual(audit['error'], 'inconsistent_adequacy')

    async def test_named_percentage_never_becomes_a_currency_amount(self):
        raw = {'analyses': [{'tool': 'projection', 'changes': [
            {'metric': 'income', 'amount': -30, 'source': ''}]}]}
        plan = await self.plan('假设翻译收入减少30%，未来三个月能存多少？', raw, {})
        self.assertEqual([(c.source,c.amount,c.percent) for c in plan.analyses[0].changes], [('翻译',None,-30)])

    async def test_unrelated_purchase_goal_and_other_ledger_cannot_inherit_projection(self):
        first = await self.plan('未来三个月能存多少？', {'analyses': [{'tool': 'projection', 'months': 3}]}, {})
        previous = await self.save(first)
        follow = await self.plan('假设培训支出减半，还能买七百元的椅子吗？',
            {'followUpOfPreviousQuestion': True, 'analyses': [{'tool': 'purchase', 'purchaseAmount': 700}]}, previous)
        self.assertNotIn('projection', [t.tool for t in follow.analyses])
        previous.pop('conversation')
        previous['scope'] = [['search_family_orders', '不限', '']]
        other = await self.plan('个人账本，培训支出减半，重新算。',
            {'followUpOfPreviousQuestion': True, 'analyses': [{'tool': 'scenario'}]}, previous)
        self.assertEqual([t.tool for t in other.analyses], ['scenario'])

    async def test_retracted_source_is_removed_before_merge_and_remains_removed_after_reload(self):
        wage = {'metric': 'income', 'source': '工资', 'percent': -50}
        other = {'metric': 'income', 'source': '稿费', 'percent': -20}
        first = await self.plan('未来三个月假设工资收入减半，稿费收入减少20%，房租不动。',
            {'analyses': [{'tool': 'projection', 'changes': [wage, other], 'protectedCategories': ['房租']}]}, {})
        prior = await self.save(first)
        self.assertEqual(len(prior['changes']), 2)
        for supplied in ([], [wage, other]):
            plan = await self.plan('不再假设工资收入减半，按正常工资重新推演。',
                {'followUpOfPreviousQuestion': True, 'analyses': [{'tool': 'projection', 'changes': supplied}]}, prior)
            self.assertEqual([(c.source,c.percent) for c in plan.analyses[0].changes], [('稿费',-20)])
            saved = await self.save(plan)
            self.assertEqual((saved['months'], saved['protectedCategories']), (3, ['房租']))
            continued = await self.plan('把期限延长一个月。', {'followUpOfPreviousQuestion': True,
                'analyses': [{'tool':'projection'}]}, saved)
            self.assertEqual([(c.source,c.percent) for c in continued.analyses[0].changes], [('稿费',-20)])
            rows = [{'year': 2026, 'month': m, 'day': 4, 'money': amount, 'costType': kind, 'orderRemark': name}
                    for m in (8,9,10) for amount,kind,name in [(5000,'工资','工资'),(1000,'收入','稿费'),(-2000,'房租','房租')]]
            results = collect_analysis_results(continued, {'个人版': [(q,rows) for q in continued.toolCalls]}, date(2026,11,19))
            evidence = next(r['evidence'] for r in results if r['tool'] == 'projection')
            self.assertEqual((evidence['monthlyNet'], evidence['totalNet']), (3800,15200))

    async def test_correction_replaces_source_and_retention_does_not_retract(self):
        wage = {'metric':'income', 'source':'工资', 'percent':-50}
        previous = await self.save(await self.plan('如果工资收入减半，未来三个月结余多少？',
            {'analyses':[{'tool':'projection', 'changes':[wage]}]}, {}))
        for phrase in ('不是工资，是稿费减半。', '不是工资而是稿费减半。'):
            plan = await self.plan(phrase, {'followUpOfPreviousQuestion':True,
                'understanding':{'referents':['稿费收入']}, 'analyses':[{'tool':'scenario', 'changes':[wage]}]}, previous)
            self.assertEqual([(c.source,c.metric,c.percent) for c in plan.analyses[0].changes], [('稿费','income',-50)])
        for phrase in ('不想改变工资收入减半的假设，继续推演。', '不要撤回工资的假设，重新算。'):
            plan = await self.plan(phrase, {'followUpOfPreviousQuestion':True, 'analyses':[{'tool':'projection'}]}, previous)
            self.assertEqual([(c.source,c.percent) for c in plan.analyses[0].changes], [('工资',-50)])
        new = await self.plan('换个问题，未来三个月的正常结余？', {'analyses':[{'tool':'projection'}]}, previous)
        self.assertFalse(new.analyses[0].changes)

    async def test_reviewer_cannot_restore_retracted_prior_change_even_with_same_source_on_other_metric(self):
        income = {'metric':'income','source':'培训','percent':-50}
        expense = {'metric':'expense','source':'培训','percent':-20}
        prior = {'scope':[['search_personal_orders','不限','']], 'changes':[income, expense], 'months':3}
        question = '不再假设培训收入减半，按正常培训收入算。'
        draft = {'followUpOfPreviousQuestion': True, 'analyses':[{'tool':'projection'}]}
        p = await self.plan(question, draft, prior)
        self.assertEqual([(c.metric,c.percent) for c in p.analyses[0].changes], [('expense',-20)])
        decision = {'adequate':False, 'issues':[], 'analysisWindow':None, 'answerInstruction':'',
                    'analyses':[{'tool':'projection', 'changes':[income,expense]}]}
        model = AsyncMock()
        model.ainvoke.return_value = SimpleNamespace(content=json.dumps(decision))
        with patch('app.agent.plan_review.get_structured_model', return_value=model):
            result, audit = await PlanReviewer().review(question, '', AgentPlan.model_validate(draft), date(2026,11,19),'个人版',prior)
        self.assertIsNone(result)
        self.assertEqual(audit['error'],'unsupported_changes')

    async def test_explicit_start_and_duration_override_only_the_requested_projection_condition(self):
        original = await self.plan('未来三个月结余多少？', {'analyses': [{'tool':'projection','months':3}]}, {})
        prior = await self.save(original)
        wage = {'metric':'income','source':'工资','percent':-50}
        for phrase, fields, expected in [
            ('从2027年2月开始，工资收入减半，按同样期限重新推演。', {'targetMonth':'2027-02'}, (3,'2027-02')),
            ('工资收入减半，按六个月重新推演。', {'months':6}, (6,'2026-12')),
            ('参考过去六个月的样本，工资收入减半，按原期限推演。', {'months':6}, (3,'2026-12')),
        ]:
            with self.subTest(phrase=phrase):
                draft = {'followUpOfPreviousQuestion':True, 'analyses':[{'tool':'projection','changes':[wage], **fields}]}
                plan = await self.plan(phrase, draft, prior)
                self.assertEqual((plan.analyses[0].months,plan.analyses[0].targetMonth), expected)
                self.assertEqual((plan._analysis_context['months'],plan._analysis_context['targetMonth']), expected)

    async def test_same_source_replacement_remains_the_next_turn_assumption(self):
        original = await self.plan('工资收入减半，未来三个月能存多少？',
            {'analyses': [{'tool':'projection','changes':[{'metric':'income','source':'工资','percent':-50}]}]}, {})
        prior = await self.save(original)
        replacement = {'metric':'income','source':'工资','percent':-20}
        plan = await self.plan('不再假设工资收入减半，改为工资收入减少20%。',
            {'followUpOfPreviousQuestion':True, 'analyses':[{'tool':'projection','changes':[replacement]}]},prior)
        saved = await self.save(plan)
        self.assertEqual([(c['source'],c['percent']) for c in saved['changes']], [('工资',-20)])
        follow = await self.plan('按刚才的情景再算。', {'followUpOfPreviousQuestion':True, 'analyses':[{'tool':'projection'}]},saved)
        self.assertEqual([(c.source,c.percent) for c in follow.analyses[0].changes], [('工资',-20)])
