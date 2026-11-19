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
    async def test_calendar_expansion_keeps_one_logical_scope_for_explicit_decision(self):
        from app.agent.planner import QueryPlanner
        from app.schemas.plan import AgentPlan
        planner = QueryPlanner.__new__(QueryPlanner)
        plan = AgentPlan(understanding={'objective': '判断购买扫描仪所缺的条件',
            'evidenceNeeds': ['totals', 'sources', 'forecast', 'quality']}, analyses=[{'tool': 'cashflow'}],
            toolCalls=[{'id': f'ledger_{month}', 'sourceIds': ['ledger'], 'toolName': 'search_personal_orders',
                        'args': {'year': 2026, 'month': month}} for month in [9, 10]])
        planner._materialize_requirements(plan, '按刚才讲的情况，我现在能直接拿七百元买扫描仪吗？还需要什么信息才能判断？')
        purchase = next((t for t in plan.analyses if t.tool == 'purchase'), None)
        self.assertIsNotNone(purchase)
        self.assertEqual(purchase.purchaseAmount, 700)
        self.assertIsNone(purchase.startingBalance)

    async def plan(self, text, draft, previous):
        planner = QueryPlanner.__new__(QueryPlanner)
        planner.chain = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps(draft))))
        with patch('app.agent.planner.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 19)
            return await planner.create_plan(text, '', '个人版', previous)

    async def save(self, plan, evidence=None):
        agent = BookAgent.__new__(BookAgent)
        agent.memory = AsyncMock()
        await agent._save_assistant_reply('synthetic', 'synthetic', '核算结果', plan, evidence=evidence)
        return latest_context(json.loads(json.dumps([agent.memory.add_message.await_args.kwargs])))

    async def test_named_shock_protected_expenses_and_extension_use_one_actual_projection(self):
        rows = [{'year': 2026, 'month': m, 'day': 4, 'money': value, 'costType': category, 'orderRemark': remark}
                for m in (8, 9, 10) for value, category, remark in
                [(5000, '工资', '工资'), (1600, '收入', '翻译稿费'), (-2600, '房租', '房租')]]
        first = await self.plan('未来三个月能存多少？', {'analyses': [{'tool': 'projection', 'months': 3}]}, {})
        previous = await self.save(first, {'ok': True, 'analysisResults': [{'mode': '个人版', 'tool': 'sources',
            'queryIds': [], 'evidence': {'sources': [{'source': '翻译稿费', 'metric': 'income'}],
                                         'catalogMetric': 'all', 'omittedSourceCount': 0}}]})
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

    async def test_source_shorthand_on_a_projection_uses_scoped_computed_catalog_evidence(self):
        question = '家教可能会少一半，重新算一下。'
        home = 'search_family_orders'
        personal = 'search_personal_orders'
        for scope, ids, rows, expected in [
            (personal, ['ledger'], [('家教课时费', 'income')], 'income'),
            (personal, ['ledger'], [('家教课时费', 'expense')], 'expense'),
            (home, ['other'], [('家教课时费', 'income')], None),
            (personal, ['other'], [('家教课时费', 'income')], None),
            (personal, ['ledger'], [('家教课时费', 'income'), ('家教课时费', 'expense')], None)]:
            with self.subTest(scope=scope, ids=ids, rows=rows):
                query = {'id': 'ledger', 'toolName': personal, 'args': {'cost_type': '不限'}}
                other = {'id': 'other', 'toolName': scope, 'args': {'cost_type': '餐饮'}}
                projection = {'tool': 'projection', 'months': 3, 'metric': 'net', 'queryIds': ['ledger']}
                ctx = {'scope': [[personal, '不限', '']], 'months': 3, 'activeAnalyses': [projection],
                       'targetMonth': '2026-12', 'conversation': {'queries': [query], 'analyses': [projection]}}
                source_rows = [{'source': name, 'metric': metric, 'amount': 2500, 'memberKey': 'private'}
                               for name, metric in rows]
                meta = {'analysisContext': ctx, 'evidence': {'ok': True, 'analysisResults': [{
                    'tool': 'sources', 'mode': '家庭版' if scope == home else '个人版', 'queryIds': ids,
                    'evidence': {'sources': source_rows, 'catalogMetric': 'net', 'omittedSourceCount': 0}}]},
                    'plan': {'toolCalls': [query, other]}}
                previous = latest_context([{'role': 'assistant', 'content': '家教课时费收入', 'metadata': meta}])
                plan = await self.plan(question, {'followUpOfPreviousQuestion': True, 'analyses': [projection],
                    'understanding': {'objective': '沿用上次情景重新推演'}}, previous)
                if expected:
                    task = next(t for t in plan.analyses if t.tool == 'projection')
                    self.assertEqual(task.months, 3)
                    self.assertEqual([(c.source, c.metric, c.percent) for c in task.changes],
                                     [('家教课时费', expected, -50)])
                    self.assertNotIn('private', str(previous))
                else:
                    self.assertFalse(plan.toolCalls)

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

    async def test_validated_corrected_execution_is_adopted_despite_a_contradictory_summary_flag(self):
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
        self.assertEqual(model.ainvoke.await_count, 1)
        self.assertEqual(audit['status'], 'reviewed')
        self.assertTrue(audit['adequacyNormalized'])

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

    async def test_validated_execution_diff_does_not_require_repeating_the_review_call(self):
        model = AsyncMock()
        inconsistent = {'adequate': True, 'issues': [], 'analysisWindow': None, 'answerInstruction': '',
                        'analyses': [{'tool': 'projection', 'months': 3}]}
        model.ainvoke.side_effect = [SimpleNamespace(content=json.dumps(inconsistent)), TimeoutError()]
        with patch('app.agent.plan_review.get_structured_model', return_value=model):
            result, audit = await PlanReviewer().review('未来三个月的结余呢？', '', AgentPlan(analyses=[{'tool':'trends'}]),
                                                       date(2026,11,19), '个人版', {})
        self.assertIsNotNone(result)
        self.assertFalse(result.adequate)
        self.assertEqual(result.analyses[0].tool, 'projection')
        self.assertEqual(model.ainvoke.await_count, 1)
        self.assertTrue(audit['adequacyNormalized'])

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

    async def test_exclusion_only_aggregate_keeps_custom_categories_and_directional_counts(self):
        question = '对照上上个月和上个月，排除房租、交通后，我的支出按月份和类别列出金额、笔数和每笔均额，各类别增加多少？'
        draft = {'toolCalls':[{'id':'mine','toolName':'search_personal_orders'}],
                 'analyses':[{'tool':'aggregate','queryIds':['mine'],'metric':'all','aggregate':{
                     'groupBy':['month','category'],'filters':{'includeCategories':['餐饮','购物','娱乐'],
                                                               'excludeCategories':['房租','交通']}}}]}
        plan = await self.plan(question,draft,{})
        task = plan.analyses[0]
        self.assertEqual((task.metric,task.queryIds,task.aggregate.filters.includeCategories),('expense',['mine'],[]))
        self.assertEqual(plan.output.metric,'expense')
        partial=json.loads(json.dumps(draft))
        partial['analyses'][0]['aggregate']['filters']['excludeCategories']=['房租','购物']
        repaired=(await self.plan(question,partial,{})).analyses[0]
        self.assertEqual(repaired.aggregate.filters.excludeCategories,['房租','交通'])
        rows = [{'year':2026,'month':m,'day':2,'costType':kind,'money':amt,'orderRemark':kind}
                for m,kind,amt in [(9,'展览',-10),(10,'展览',-50),(10,'展览',-10),(10,'展览',700),(10,'房租',-800)]]
        data = collect_analysis_results(plan,{'个人版':[(q,rows) for q in plan.toolCalls]},date(2026,11,19))
        e = next(r['evidence'] for r in data if r['tool']=='aggregate')
        october = next(r for r in e['rows'] if r['category']=='展览' and r['month']=='2026-10')
        self.assertEqual((october['amount'],october['count'],october['meanTransactionAmount'],e['comparisons'][0]['change']),(60,2,30,50))
        for text,metric in [('排除房租后，只看展览的收入。','income'),('排除房租后，只看展览的收入和支出。','all')]:
            custom = json.loads(json.dumps(draft))
            filters = custom['analyses'][0]['aggregate']['filters']
            filters.update(includeCategories=['展览','购物'],excludeCategories=['房租'])
            t=(await self.plan(text,custom,{})).analyses[0]
            self.assertEqual((t.metric,t.aggregate.filters.includeCategories),(metric,['展览']))

    async def test_monthly_improvement_materializes_optimization_with_protections_without_a_cut(self):
        question = '房租和交通维持原状，我每个月还想多留三百，给我可调整开销和差多少。'
        for include_projection in (False,True):
            tasks=[{'tool':'scenario','changes':[{'metric':'expense','amount':-300}]},{'tool':'changes'}]
            if include_projection:
                tasks.append({'tool':'projection','months':3})
            plan=await self.plan(question,{'analyses':tasks},{})
            opt=next((t for t in plan.analyses if t.tool=='optimization'),None)
            self.assertIsNotNone(opt)
            self.assertEqual((opt.monthlyImprovement,opt.protectedCategories,opt.changes),(300,['房租','交通'],[]))
            self.assertNotIn('scenario',[t.tool for t in plan.analyses])
            self.assertEqual(include_projection, any(t.tool=='projection' for t in plan.analyses))

    async def test_mixed_ledger_aggregate_metrics_and_decisions_are_not_globally_rewritten(self):
        draft={'toolCalls':[{'id':'mine','toolName':'search_personal_orders'},{'id':'home','toolName':'search_family_orders'}],
               'analyses':[{'tool':'aggregate','queryIds':['mine'],'metric':'income'}, {'tool':'aggregate','queryIds':['home'],'metric':'expense'}]}
        p=await self.plan('比较我和家庭的收入和支出，每月多留三百的想法先记录。',draft,{})
        self.assertEqual([(t.queryIds,t.metric) for t in p.analyses],[(['mine'],'income'),(['home'],'expense')])

    async def test_calendar_prompt_and_answer_instructions_agree_with_resolved_historical_window(self):
        question='对照上上个月和上个月，按类别列出支出和笔数。'
        draft={'analysisWindow':{'startDate':'2026-10-01','endDate':'2026-11-30'},
               'understanding':{'objective':'对比2026年10月和11月的分类支出','requestedAnswers':['列出2026-10和2026-11各类金额和笔数'],
                                'referents':['上上个月：2026-10','上个月：2026-11']},
               'finalInstruction':'汇报2026-10到2026-11的分类金额','analyses':[{'tool':'aggregate'}]}
        planner=QueryPlanner.__new__(QueryPlanner)
        planner.chain=SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps(draft))))
        with patch('app.agent.planner.datetime',wraps=datetime) as clock:
            clock.now.return_value=datetime(2026,11,19)
            p=await planner.create_plan(question,'','个人版',{})
        args=planner.chain.ainvoke.await_args.args[0]
        self.assertIn('2026-09',str(args.get('calendar_context','')))
        self.assertIn('2026-10',str(args.get('calendar_context','')))
        descriptions=' '.join([p.understanding.objective,p.finalInstruction,*p.understanding.referents,*p.understanding.requestedAnswers])
        self.assertNotIn('2026-11',descriptions)
        self.assertNotIn('11月',descriptions)
        self.assertNotIn('上上个月：2026-10',descriptions)
        self.assertIn('2026-09',descriptions)
        self.assertIn('笔数',descriptions)

    async def test_placeholder_filters_do_not_zero_a_weekday_followup_or_overall_diagnosis(self):
        from app.tools.financial_analysis import run_analysis
        rows = [{'year': 2026, 'month': 10, 'day': 7, 'money': -amount, 'costType': category,
                 'memberName': '春枝'} for category, amount in [('展览', 180), ('交通', 20), ('外食', 40)]]
        for question, filters, applied, expense in [
            ('沿用这两个月的范围，只看展览，按星期几分组，哪天的已记支出最多？',
             {'includeCategories': ['展览'], 'excludeCategories': ['交通'], 'includeMembers': ['不限'], 'minAmount': 0},
             {'includeCategories': ['展览'], 'excludeCategories': ['交通']}, 180),
            ('综合看看最近半年的财务情况，按星期列出总额。',
             {'includeCategories': ['all'], 'includeMembers': ['ALL'], 'includeSources': ['all'],
              'excludeCategories': ['不限'], 'excludeMembers': ['无筛选'], 'excludeSources': ['全部'],
              'remarkContains': 'none', 'excludeRemarks': ['null'], 'minAmount': 0, 'maxAmount': 0},
             {}, 240)]:
            with self.subTest(question=question):
                draft = {'toolCalls': [{'id': 'mine', 'toolName': 'search_personal_orders'}],
                         'analyses': [{'tool': 'aggregate', 'metric': 'all', 'queryIds': ['mine'],
                                      'aggregate': {'groupBy': ['weekday'], 'filters': filters}}]}
                plan = await self.plan(question, draft, {})
                task = next(t for t in plan.analyses if t.tool == 'aggregate')
                self.assertEqual(task.aggregate.filters.model_dump(exclude_defaults=True), applied)
                self.assertEqual(task.queryIds, ['mine'])
                result = run_analysis('aggregate', rows, as_of=date(2026, 11, 19), metric=task.metric,
                                      aggregate=task.aggregate.model_dump())
                self.assertEqual(result['totals']['expense'], expense)

    async def test_placeholder_names_are_literal_only_in_the_dimension_the_user_named(self):
        for question, raw, expected in [
            ('只看分类名称为“全部”的账单，所有成员都要统计。',
             {'includeCategories': ['全部'], 'includeMembers': ['全部']}, {'includeCategories': ['全部']}),
            ('只统计来源叫 all、备注包含“none”的账单。',
             {'includeSources': ['all'], 'remarkContains': 'none', 'includeMembers': ['all']},
             {'includeSources': ['all'], 'remarkContains': 'none'}),
            ('排除“none”类别后，按成员列出。',
             {'excludeCategories': ['none'], 'excludeMembers': ['none']}, {'excludeCategories': ['none']}),
            ('仅统计金额为零的账单。', {'maxAmount': 0}, {'maxAmount': 0})]:
            with self.subTest(question=question):
                plan = await self.plan(question, {'analyses': [{'tool': 'aggregate',
                    'aggregate': {'groupBy': ['member'], 'filters': raw}}]}, {})
                self.assertEqual(plan.analyses[0].aggregate.filters.model_dump(exclude_defaults=True), expected)

    async def test_reviewer_cannot_add_unrequested_category_whitelist(self):
        original={'tool':'aggregate','metric':'expense','aggregate':{'groupBy':['category'],'filters':{'excludeCategories':['通勤']}}}
        changed=json.loads(json.dumps(original))
        changed['aggregate']['filters']['includeCategories']=['衣物','饮食']
        response={'adequate':False,'issues':[],'analysisWindow':None,'answerInstruction':'','analyses':[changed]}
        model=AsyncMock()
        model.ainvoke.return_value=SimpleNamespace(content=json.dumps(response))
        with patch('app.agent.plan_review.get_structured_model',return_value=model):
            result,audit=await PlanReviewer().review('排除通勤后统计各类支出。','',AgentPlan(analyses=[original]),
                                                     date(2026,11,19),'个人版',{})
        self.assertIsNone(result)
        self.assertEqual(audit.get('error'),'unsupported_includeCategories')

    async def test_independent_decisions_use_default_history_unless_user_bounds_the_sample(self):
        made_up={'startDate':'2026-09-01','endDate':'2026-11-18'}
        for tool, question in [
            ('forecast','预计2027年1月能进账多少？'), ('forecast','预计明年一月收入多少？'),
            ('forecast','按我的账本，估算今年12月的入账。'), ('forecast','预计本月收入多少？'),
            ('scenario','未来收入有变时，结余会怎样？'), ('savings','我想三个月内攒到一万元。'),
            ('projection','估算未来三个月收支。'), ('purchase','今天想买七百元的扫描仪，可行吗？'),
            ('runway','可动用余额能支撑多久？'),('optimization','我最近想优化开销。'),
        ]:
            with self.subTest(tool=tool,question=question):
                p=await self.plan(question,{'analysisWindow':made_up,'analyses':[{'tool':tool}]},{})
                self.assertIsNone(p.analysisWindow)
                self.assertEqual([q.args.month for q in p.toolCalls],[5,6,7,8,9,10,11])
        for phrase, start,end in [
            ('参考最近两个完整月','2026-09-01','2026-10-31'), ('仅参考今年的流水','2026-01-01','2026-11-19'),
            ('只用去年记录','2025-01-01','2025-12-31'), ('只用2024年的账本','2024-01-01','2024-12-31'),
            ('按2026-08-02至2026-09-28的记录','2026-08-02','2026-09-28')]:
            with self.subTest(phrase=phrase):
                defined={'startDate':start,'endDate':end}
                plan=await self.plan(phrase+'，预计2027年1月收入多少？',
                    {'analyses':[{'tool':'forecast'}],'analysisWindow':defined},{})
                self.assertEqual(plan.analysisWindow.model_dump(mode='json'),defined)
                prior=await self.save(plan)
                follow=await self.plan('把刚才的预测改到明年三月，仍用原样本。',{'followUpOfPreviousQuestion':True,
                    'analyses':[{'tool':'forecast'}],'analysisWindow':made_up},prior)
                self.assertEqual(follow.analysisWindow.model_dump(mode='json'),defined)

    async def test_recent_half_year_diagnosis_materializes_six_complete_month_averages(self):
        draft={'understanding':{'evidenceNeeds':['totals','trends','changes']}, 'analysisType':'financial_analysis',
               'analyses':[{'tool':'aggregate','metric':'all'},{'tool':'trends'},{'tool':'anomalies'}],
               'analysisWindow':{'startDate':'2026-05-19','endDate':'2026-11-18'}}
        rows=[{'year':2026,'month':m,'day':4,'money':m*100,'costType':'工资','orderRemark':'工资'} for m in range(5,12)]
        for time in ('最近半年','近半年'):
            plan=await self.plan('综合看看我'+time+'的财务情况，最值得注意什么？',draft,{})
            self.assertEqual(plan.analysisWindow.model_dump(mode='json'),{'startDate':'2026-05-01','endDate':'2026-11-19'})
            result=collect_analysis_results(plan,{'个人版':[(q,rows) for q in plan.toolCalls]},date(2026,11,19))
            cash=next((r['evidence'] for r in result if r['tool']=='cashflow'),None)
            self.assertIsNotNone(cash)
            self.assertEqual(cash['completeMonthAverage']['months'],['2026-%02d'%m for m in range(5,11)])
            self.assertEqual(cash['completeMonthAverage']['income'],750)
        short=await self.plan('只看近半年的支出类别。',draft,{})
        self.assertNotIn('cashflow',[t.tool for t in short.analyses])
        future=await self.plan('未来半年能攒多少结余？',{**draft,'analyses':[{'tool':'projection','months':6}]},{})
        self.assertNotIn('cashflow',[t.tool for t in future.analyses])
        self.assertIsNone(future.analysisWindow)

    async def test_canonical_runway_and_savings_compute_or_report_only_actual_inputs(self):
        rows=[{'year':2026,'month':m,'day':2,'money':v,'costType':category,'orderRemark':category}
              for m in (8,9,10) for v,category in [(6000,'工资'),(-2000,'开销')]]
        for text, need, missing in [
            ('如果主要收入来源出了问题，我能承受多久？','runway',{'startingBalance','changes'}),
            ('请评估我的储蓄目标，还需要哪些条件？','savings',{'startingBalance','targetAmount'}),
            ('假设现在有10000元，想三个月存到20000元，照目前收支能做到吗？','savings',set())]:
            with self.subTest(need=need,text=text):
                p=await self.plan(text,{'analyses':[{'tool':'trends'}],'understanding':{'evidenceNeeds':['totals',need]}},{})
                t=next((t for t in p.analyses if t.tool==need),None)
                self.assertIsNotNone(t)
                self.assertFalse(t.changes)
                self.assertNotIn('cashflow',[t.tool for t in p.analyses])
                e=next(r['evidence'] for r in collect_analysis_results(p,{'个人版':[(q,rows) for q in p.toolCalls]},date(2026,11,19)) if r['tool']==need)
                if missing:
                    self.assertIsNone(t.startingBalance)
                    self.assertEqual(set(e['missingInputs']),missing)
                else:
                    self.assertEqual((t.startingBalance,t.targetAmount,t.months,e['projectedBalance']),(10000,20000,3,22000))

    async def test_canonical_need_mapping_deduplicates_and_never_invents_scope(self):
        aliases={'projection':'projection','budget':'budget','purchase':'purchase','optimization':'optimization',
                 'quality':'data_quality','data_quality':'data_quality','cashflow':'cashflow','income_stability':'income_stability','recurring':'recurring','anomalies':'anomalies',
                 'aggregate':'aggregate','scenario':'scenario','members':'sources','remarks':'sources','forecast':'forecast','changes':'changes'}
        questions={'projection':'未来三个月按当前收支能攒下多少？','budget':'本月预算还剩多少？',
                   'purchase':'现在买这台相机合适吗？','optimization':'怎样调整开支才能多留一些？',
                   'forecast':'下个月预计能进账多少？','scenario':'假设所有收入都中断，结余会怎样？'}
        for need,tool in aliases.items():
            plan=await self.plan(questions.get(need,'核对已说明的历史数据'),{'understanding':{'evidenceNeeds':[need,need]}},{})
            self.assertEqual([t.tool for t in plan.analyses].count(tool),1,need)
        modes=[{'id':'mine','toolName':'search_personal_orders'},{'id':'home','toolName':'search_family_orders'}]
        plan=await self.plan('两个账本分开核对',{'toolCalls':modes,'understanding':{'evidenceNeeds':['runway','savings','forecast']},
            'analyses':[{'tool':'savings','queryIds':['mine']}]},{})
        self.assertEqual([(t.tool,t.queryIds) for t in plan.analyses],[('savings',['mine'])])

    async def test_new_decision_tasks_do_not_treat_old_balances_or_pay_frequency_as_goal_conditions(self):
        p=await self.plan('去年可用现金为10000元。每三个月领一次奖金，现在想了解存款目标还要什么信息？',
            {'understanding':{'evidenceNeeds':['savings']}},{})
        t=next(t for t in p.analyses if t.tool=='savings')
        self.assertIsNone(t.startingBalance)
        self.assertIsNone(t.months)
        chat=await self.plan('你好',{'intent':'chat','needsTools':False,'understanding':{'evidenceNeeds':['runway']}},{})
        self.assertFalse(chat.needsTools)

    async def test_family_income_direction_spans_raw_categories_and_limits_the_source_catalog(self):
        query={'id':'home','toolName':'search_family_orders'}
        aggregate={'tool':'aggregate','queryIds':['home'],'metric':'income','aggregate':{
            'groupBy':['member','source'],'filters':{'includeCategories':['收入']}}}
        draft={'toolCalls':[query],'output':{'metric':'all'},'analyses':[aggregate,{'tool':'sources','queryIds':['home']}]}
        plan=await self.plan('在我们家上一个完整月的收入里，谁从哪些地方收到了钱？请按人分清楚。',draft,{})
        task=plan.analyses[0]
        self.assertEqual((task.aggregate.filters.includeCategories,task.queryIds,task.metric,plan.output.metric),([],['home'],'income','income'))
        rows=[{'year':2026,'month':10,'day':3,'money':amount,'costType':category,'orderRemark':remark,'memberKey':member,'memberName':member}
              for amount,category,remark,member in [(5300,'收入','工资','甲'),(6000,'项目','项目进度','甲'),(2950,'配送','项目进度','乙'),(-600,'采购','配件','乙')]]
        result=collect_analysis_results(plan,{'家庭版':[(c,rows) for c in plan.toolCalls]},date(2026,11,19))
        data=next(r['evidence'] for r in result if r['tool']=='aggregate')
        self.assertEqual(data['totals']['income'],14250)
        self.assertEqual({r['member'] for r in data['rows']},{'甲','乙'})
        catalog=next(r['evidence'] for r in result if r['tool']=='sources')
        self.assertEqual(catalog['catalogMetric'],'income')
        self.assertEqual({r['metric'] for r in catalog['sources']},{'income'})

    async def test_literal_direction_named_category_and_real_category_survive_with_their_independent_metric(self):
        examples=[
            ('只看分类名称为“收入”的所有账单。','includeCategories',['收入'],'all'),
            ('排除类别叫支出的账单后，统计收入。','excludeCategories',['支出'],'income'),
            ('只看工资类别的进账。','includeCategories',['工资'],'income'),
            ('排除支出后统计所有进账。','excludeCategories',[],'income'),
            ('比较收入和支出。','includeCategories',[],'all'),
        ]
        for question,field,expected,metric in examples:
            with self.subTest(question=question):
                values=expected if expected else (['支出'] if field=='excludeCategories' else ['收入'])
                draft={'analyses':[{'tool':'aggregate','metric':'all','aggregate':{'groupBy':['category'],'filters':{field:values}}},{'tool':'sources'}]}
                plan=await self.plan(question,draft,{})
                t=plan.analyses[0]
                self.assertEqual((getattr(t.aggregate.filters,field), t.metric,plan.output.metric),(expected,metric,metric))
        only_sources=await self.plan('请按人列出家庭进账的来源。',{'analyses':[{'tool':'sources'}]}, {})
        self.assertEqual(only_sources.output.metric,'income')

    async def test_reviewer_cannot_add_direction_as_an_unrequested_literal_category(self):
        for key,value in [('includeCategories','进账'),('excludeCategories','开销')]:
            t={'tool':'aggregate','metric':'income','aggregate':{'filters':{key:[value]}}}
            decision={'adequate':False,'issues':[],'answerInstruction':'','analysisWindow':None,'analyses':[t]}
            model=AsyncMock()
            model.ainvoke.return_value=SimpleNamespace(content=json.dumps(decision))
            with patch('app.agent.plan_review.get_structured_model',return_value=model):
                result,audit=await PlanReviewer().review('核算进账来源。','',AgentPlan(analyses=[{'tool':'aggregate','metric':'income'}]),
                                                        date(2026,11,19),'家庭版',{})
            self.assertIsNone(result)
            self.assertEqual(audit['error'],'unsupported_'+key)

    async def test_monthly_spending_goal_does_not_acquire_optional_unasked_financial_decisions(self):
        q='沿用刚才两个月，场租和通勤保留。我想每月多留三百，哪些地方可调整，还差多少？'
        needs=['totals','changes','sources','budget','optimization','savings','purchase','scenario','runway']
        draft={'understanding':{'evidenceNeeds':needs,'requestedAnswers':['哪些开支可以调整','当前余额距离存款目标的差额（若涉及）','现金能撑多久（若涉及）'],
                               'unknowns':['存款目标余额','待确认的支出安排']},
               'analyses':[{'tool':t} for t in ['optimization','changes','budget','purchase','savings','scenario','runway']]}
        p=await self.plan(q,draft,{})
        forbidden={'budget','purchase','savings','scenario','runway'}
        self.assertFalse(forbidden & {t.tool for t in p.analyses})
        self.assertFalse(forbidden & set(p.understanding.evidenceNeeds))
        self.assertEqual(p.understanding.requestedAnswers,['哪些开支可以调整'])
        self.assertNotIn('存款目标余额',p.understanding.unknowns)
        opt=next(t for t in p.analyses if t.tool=='optimization')
        self.assertEqual((opt.monthlyImprovement,opt.protectedCategories,opt.changes),(300,['场租','通勤'],[]))
        combined=await self.plan(q+'另外想花700元买扫描仪，可行吗？',draft,{})
        self.assertIn('purchase',[t.tool for t in combined.analyses])

    async def test_protected_spending_and_deadline_followups_survive_aggregate_only_and_duplicate_projections(self):
        rows=[{'year':2026,'month':m,'day':2,'money':value,'costType':cat,'orderRemark':name}
              for m in (8,9,10) for value,cat,name in [(5000,'工资','工资'),(1600,'家教','家教'),(-2200,'房租','房租'),(-400,'餐饮','午饭')]]
        sh={'metric':'income','percent':-50,'source':'家教'}
        initial=await self.plan('未来三个月能攒多少？',{'analyses':[{'tool':'projection','months':3}]},{})
        shocked=await self.plan('家教收入如果减少一半，重算。',{'followUpOfPreviousQuestion':True,'analyses':[{'tool':'projection','changes':[sh]}]},await self.save(initial))
        # The reviewer chose historical exclusion filters instead of protected decision inputs.
        supported=await self.plan('房租不能动，吃饭也不想再省，其他地方还能怎么调整？',
            {'followUpOfPreviousQuestion':True,'analyses':[{'tool':'aggregate','metric':'expense','aggregate':{
                'groupBy':['category'],'filters':{'excludeCategories':['房租','餐饮']}}},{'tool':'changes'}],
             'understanding':{'objective':'保持房租和餐饮，优化其他开支','referents':['固定条件：房租（不可动）、餐饮（不想再省）'],
                              'evidenceNeeds':['aggregate','changes','trends']}},await self.save(shocked))
        self.assertIn('optimization',[t.tool for t in supported.analyses])
        saved=await self.save(supported)
        self.assertTrue({'房租','餐饮'} <= set(saved['protectedCategories']))
        self.assertIn('projection',[t['tool'] for t in saved['activeAnalyses']])
        extended=await self.plan('那把目标推迟两个月，压力能小多少？',{'followUpOfPreviousQuestion':True,
            'understanding':{'evidenceNeeds':['projection','scenario']},'analyses':[
                {'tool':'projection','months':3,'queryIds':['ledger']},{'tool':'projection','months':5,'queryIds':['ledger']},
                {'tool':'scenario','months':3}]},saved)
        current=await self.save(extended)
        self.assertFalse(current.get('ambiguous'))
        self.assertEqual((current['months'],current['previousMonths']),(5,3))
        self.assertTrue({'房租','餐饮'} <= set(current['protectedCategories']))
        projection=next(t for t in extended.analyses if t.tool=='projection')
        self.assertEqual([(c.metric,c.source,c.percent) for c in projection.changes],[('income','家教',-50)])
        result=collect_analysis_results(extended,{'个人版':[(t,rows) for t in extended.toolCalls]},date(2026,11,19))
        amounts=[(r['evidence']['months'],r['evidence']['totalNet']) for r in result if r['tool']=='projection']
        self.assertEqual(amounts,[(5,16000)])

    async def test_old_balance_is_not_current_and_does_not_make_savings_plan_unexecutable(self):
        for draft in ({'tool':'savings'}, {'tool':'savings','startingBalance':10000}):
            p=await self.plan('去年余额为10000元。今年想了解存款目标，还需要哪些信息？',{'analyses':[draft]},{})
            self.assertEqual([t.tool for t in p.analyses],['savings'])
            self.assertIsNone(p.analyses[0].startingBalance)

    async def test_history_goals_do_not_create_any_tag_only_decisions(self):
        decisions={'forecast','scenario','projection','purchase','budget','savings','runway','optimization'}
        for question in ('这个月收入主要从哪里来？','我这个月买过哪些东西？','上个月我买过扫描仪吗？','只列出本月收入，不用预测下个月。'):
            draft={'understanding':{'objective':'解释本月收入来源','requestedAnswers':['说明收入构成'],
                                    'evidenceNeeds':['sources',*sorted(decisions)]},'analyses':[{'tool':'sources'}]}
            with self.subTest(question=question):
                p=await self.plan(question,draft,{})
                self.assertFalse(decisions & {t.tool for t in p.analyses})
                self.assertFalse(decisions & set(p.understanding.evidenceNeeds))
        totals=await self.plan('回顾整体财务情况和收支合计。',
            {'understanding':{'evidenceNeeds':['totals','purchase']},'analyses':[{'tool':'trends'}]}, {})
        self.assertIn('cashflow',[t.tool for t in totals.analyses])
        self.assertNotIn('purchase',[t.tool for t in totals.analyses])

    async def test_a_referenced_scoped_decision_can_be_recovered_but_not_transferred_to_another_ledger(self):
        first=await self.plan('下个月预算还剩多少？',{'analyses':[{'tool':'budget','budgetAmount':300}]},{})
        saved=await self.save(first)
        draft={'followUpOfPreviousQuestion':True,'understanding':{'objective':'展开刚才的核算','evidenceNeeds':['budget','purchase']},
               'analyses':[{'tool':'trends'}]}
        follow=await self.plan('把上次的结果再算一遍。',draft,saved)
        self.assertEqual([t.tool for t in follow.analyses if t.tool in {'budget','purchase'}],['budget'])
        other=await self.plan('家庭账本也按同样方式展开。',{**draft,'toolCalls':[{'id':'home','toolName':'search_family_orders'}]},saved)
        self.assertFalse({t.tool for t in other.analyses} & {'budget','purchase'})

    async def test_explicit_new_goal_and_referenced_budget_are_both_executed_only_in_the_original_scope(self):
        first=await self.plan('我本月预算300元，还剩多少？',{'analyses':[{'tool':'budget','budgetAmount':300}]},{})
        saved=await self.save(first)
        draft={'followUpOfPreviousQuestion':True,'analyses':[{'tool':'forecast','metric':'income'}],
               'understanding':{'evidenceNeeds':['budget','forecast']}}
        combined='把上次的结果再算一遍，另外预测下个月收入。'
        follow=await self.plan(combined,draft,saved)
        self.assertEqual({t.tool for t in follow.analyses},{'budget','forecast'})
        self.assertEqual(next(t for t in follow.analyses if t.tool=='budget').budgetAmount,300)
        self.assertEqual(next(t for t in follow.analyses if t.tool=='forecast').targetMonth,'2026-12')
        for question, extra in [
            ('预测下个月收入。',{'followUpOfPreviousQuestion':False}),
            ('预测下个月收入。',{}),
            (combined,{'resetAnalysisContext':True}),
            ('换到家庭账本，'+combined,{'toolCalls':[{'id':'home','toolName':'search_family_orders'}]}),
        ]:
            with self.subTest(question=question,extra=extra):
                plan=await self.plan(question,{**draft,'analyses':[{'tool':'trends'}],**extra},saved)
                self.assertIn('forecast',{t.tool for t in plan.analyses})
                self.assertNotIn('budget',{t.tool for t in plan.analyses})

    async def test_an_existing_purchase_task_recovers_the_explicit_price_but_not_a_unit_count(self):
        for question, price in [
            ('我下个月想买一台6000元的电脑，会不会影响日常生活？', 6000),
            ('我想买一台电脑，能承受吗？', None),
            ('去年花6000元买过电脑。现在想买一台新电脑，能承受吗？', None)]:
            with self.subTest(question=question):
                p = await self.plan(question, {'analyses': [{'tool': 'purchase', 'purchaseAmount': None}]}, {})
                t = next(t for t in p.analyses if t.tool == 'purchase')
                self.assertEqual(t.purchaseAmount, price)
                self.assertIsNone(t.startingBalance)

    async def test_savings_horizon_consumes_the_whole_duration(self):
        for phrase, months in [('一年半', 18), ('两年', 24), ('一年零三个月', 15), ('半年', 6)]:
            with self.subTest(phrase=phrase):
                for draft_months in [months, None]:
                    prepared = await self.plan(f'我现在有10000元，想在{phrase}内存到20000元。',
                        {'analyses': [{'tool': 'savings', 'months': draft_months}]}, {})
                    self.assertEqual(prepared.analyses[0].months, months)
        previous_years = await self.plan('以往两年的收支作参考，现在有10000元，多久能存到20000元？',
                                        {'analyses': [{'tool': 'savings', 'months': 24}]}, {})
        self.assertIsNone(previous_years.analyses[0].months)

    async def test_the_current_single_forecast_uses_the_requested_direction(self):
        first = '按个人账本预计2027年1月能进账多少？同一项目的不同工序算一个收入来源。'
        for question, raw, expected in [(first, 'net', 'income'), ('下个月会有多少开销？', 'income', 'expense'),
                ('预计下个月的净收入是多少？', 'net', 'net')]:
            with self.subTest(question=question):
                plan = await self.plan(question, {'analyses': [{'tool': 'sources'},
                    {'tool': 'forecast', 'metric': raw, 'targetMonth': '2027-01'}]}, {})
                self.assertEqual(next(t for t in plan.analyses if t.tool == 'forecast').metric, expected)
        separate = [{'tool': 'forecast', 'metric': metric} for metric in ('income', 'expense')]
        for question in ('预计下个月收入和支出各是多少？', '两种情景分别预测下月收入。'):
            plan = await self.plan(question, {'analyses': separate}, {})
            self.assertEqual([t.metric for t in plan.analyses], ['income', 'expense'])

    async def test_new_grouping_can_combine_contiguous_referenced_periods_without_widening_gaps(self):
        for october, days_apart in [('2026-10', False), ('2026-11', True)]:
            periods = [{'label': '甲', 'startDate': '2026-09-01', 'endDate': '2026-09-30'},
                       {'label': '乙', 'startDate': october+'-01', 'endDate': october+('-30' if days_apart else '-31')}]
            window = {'startDate': periods[0]['startDate'], 'endDate': periods[-1]['endDate']}
            old = {'tool': 'aggregate', 'metric': 'expense', 'queryIds': ['ledger'], 'aggregate': {
                'groupBy': ['category'], 'periods': periods}}
            previous = {'scope': [['search_personal_orders', '不限', '']], 'conversation': {
                'analysisWindow': window, 'analyses': [old], 'queries': [{'id': 'ledger', 'toolName': 'search_personal_orders'}]}}
            for clause, by_period in [('按星期几分组，哪天最多？', False), ('分别按月比较各星期的支出。', True)]:
                with self.subTest(october=october, clause=clause):
                    raw = {'followUpOfPreviousQuestion': True, 'analyses': [{**old, 'aggregate': {
                        'groupBy': ['weekday'], 'periods': [] if days_apart else periods,
                        'filters': {'includeCategories': ['展览']}}}]}
                    plan = await self.plan('沿用这两个月的范围，只看展览，'+clause, raw, previous)
                    task = next(t for t in plan.analyses if t.tool == 'aggregate')
                    self.assertEqual(task.aggregate.filters.includeCategories, ['展览'])
                    self.assertEqual(task.queryIds, ['ledger'])
                    self.assertEqual(plan.analysisWindow.model_dump(mode='json'), window)
                    self.assertEqual([p.model_dump(mode='json') for p in task.aggregate.periods],
                                     periods if by_period or days_apart else [])

                    if not by_period and not days_apart:
                        rows = [{'year': 2026, 'month': month, 'day': day, 'money': -amount, 'costType': '展览'}
                                for month, day, amount in [(9, 5, 70), (10, 3, 90), (10, 4, 110)]]
                        results = collect_analysis_results(plan, {'个人版': [(q, rows) for q in plan.toolCalls]}, date(2026, 11, 19))
                        evidence = next(r['evidence'] for r in results if r['tool'] == 'aggregate')
                        leader = max(evidence['rows'], key=lambda row: row['expense'])
                        self.assertEqual((leader['weekday'], leader['expense'], leader['count']), ('星期六', 160, 2))
        september_week = [{'label': '展期', 'startDate': '2026-09-07', 'endDate': '2026-09-13'}]
        narrowed = await self.plan('沿用这两个月的范围，只看9月7日到13日的展览，按星期几分组。',
            {'followUpOfPreviousQuestion': True, 'analysisWindow': window, 'analyses': [
                {**old, 'aggregate': {'groupBy': ['weekday'], 'periods': september_week}}]}, previous)
        actual = next(t for t in narrowed.analyses if t.tool == 'aggregate')
        self.assertEqual([p.model_dump(mode='json') for p in actual.aggregate.periods], september_week)

    async def test_open_savings_duration_drops_invented_deadlines_and_keeps_grounded_ones(self):
        question = '现在有10000元，保持当前收支，多久能存到20000元？'
        draft = {'analyses': [{'tool': 'savings', 'months': 12}]}
        for text, proposed, expected in [(question, 12, None),
                ('以过去12个月的收支为参考。'+question, 12, None),
                ('我每3个月领一次奖金。'+question, 3, None),
                ('现在有10000元，希望三个月内存到20000元。', 12, 3)]:
            with self.subTest(text=text):
                plan = await self.plan(text, {'analyses': [{'tool': 'savings', 'months': proposed}]}, {})
                self.assertEqual(plan.intent, 'analyze_orders')
                task = next(t for t in plan.analyses if t.tool == 'savings')
                self.assertEqual((task.months, plan._analysis_context.get('months')), (expected, expected))
                self.assertTrue(any(i.get('field') == 'months' for i in plan._planning_issues))
        sibling = await self.plan(question, {'analyses': [{'tool': 'savings'},
            {'tool': 'optimization', 'months': 12}]}, {})
        self.assertIsNone(sibling._analysis_context.get('months'))
        self.assertIsNone(next(t for t in sibling.analyses if t.tool == 'savings').months)
        first = await self.plan('现在有10000元，希望三个月内存到20000元。',
                                {'analyses': [{'tool': 'savings', 'months': 3}]}, {})
        previous = await self.save(first)
        follow = await self.plan('沿用原来的目标，再算一次。', {**draft, 'followUpOfPreviousQuestion': True}, previous)
        self.assertEqual(next(t for t in follow.analyses if t.tool == 'savings').months, 3)

    async def test_deadline_change_reports_the_active_goal_and_keeps_optimization_as_evidence(self):
        for primary, amount in [('projection', None), ('savings', 20000)]:
            with self.subTest(primary=primary):
                old = {'tool': primary, 'months': 3, 'targetAmount': amount}
                tasks = [old, {'tool': 'optimization', 'protectedCategories': ['房租']}]
                prior = {'scope': [['search_personal_orders', '不限', '']], 'months': 3,
                         'targetAmount': amount, 'activeAnalyses': tasks, 'protectedCategories': ['房租'],
                         'conversation': {'analyses': tasks, 'queries': [{'id': 'ledger','toolName': 'search_personal_orders'}]}}
                raw = {'followUpOfPreviousQuestion': True, 'analyses': [{'tool': 'optimization'}, {'tool': 'projection'}]}
                plan = await self.plan('那把目标推迟两个月，压力能小多少？', raw, prior)
                main = [t for t in plan.analyses if not t._supplemental]
                self.assertEqual([t.tool for t in main], [primary])
                self.assertEqual((main[0].months, main[0].targetAmount), (5, amount))
                self.assertEqual(plan._analysis_context['previousMonths'], 3)
                self.assertEqual(next(t for t in plan.analyses if t.tool == 'optimization').protectedCategories, ['房租'])

    async def test_savings_duration_is_the_decision_even_when_the_model_chose_only_forecast(self):
        draft = {'understanding': {'evidenceNeeds': ['cashflow', 'trends', 'forecast']},
                 'analyses': [{'tool': 'cashflow'}, {'tool': 'trends'}, {'tool': 'forecast'}]}
        question = '现在有10000元，保持当前收支，多久能存到20000元？'
        plan = await self.plan(question, draft, {})
        self.assertEqual({t.tool for t in plan.analyses}, {'cashflow', 'trends', 'savings'})
        goal = next(t for t in plan.analyses if t.tool == 'savings')
        self.assertEqual((goal.startingBalance, goal.targetAmount), (10000, 20000))
        both = await self.plan(question+'另外预测下个月收入。', draft, {})
        self.assertIn('forecast', {t.tool for t in both.analyses})

    async def test_stated_purchase_is_computed_even_when_both_model_tool_lists_miss_it(self):
        question='我下个月想花6000元买电脑，现在买合适吗？会影响后面的生活吗？'
        draft={'understanding':{'objective':'评估购买电脑的可行性及生活影响','requestedAnswers':['判断是否合适'],
                  'evidenceNeeds':['cashflow','savings','budget','runway']},'analyses':[
                  {'tool':'cashflow','queryIds':['ledger'],'metric':'all'},{'tool':'budget','targetMonth':'2026-11'},
                  {'tool':'runway','targetMonth':'2026-12'},{'tool':'savings','targetMonth':'2026-12'}]}
        p=await self.plan(question,draft,{})
        self.assertEqual({t.tool for t in p.analyses},{'purchase','cashflow'})
        self.assertEqual(p.understanding.evidenceNeeds,['cashflow'])
        purchase=next(t for t in p.analyses if t.tool=='purchase')
        self.assertEqual((purchase.purchaseAmount,purchase.startingBalance),(6000,None))
        rows=[{'year':2026,'month':m,'day':1,'costType':'工资','money':3000} for m in (8,9,10)]
        result=collect_analysis_results(p,{'个人版':[(q,rows) for q in p.toolCalls]},date(2026,11,19))
        e=next(r['evidence'] for r in result if r['tool']=='purchase')
        self.assertEqual(e['purchaseAmount'],6000)
        self.assertIn('startingBalance',e['missingInputs'])

    async def test_unlisted_savings_goal_keeps_historical_support_and_uses_the_stated_conditions(self):
        draft={'understanding':{'objective':'核算是否可达到目标','evidenceNeeds':['trends']},'analyses':[{'tool':'trends'}]}
        p=await self.plan('现在有10000元，想三个月存到20000元，能做到吗？',draft,{})
        self.assertEqual({t.tool for t in p.analyses},{'savings','trends'})
        t=next(t for t in p.analyses if t.tool=='savings')
        self.assertEqual((t.startingBalance,t.targetAmount,t.months),(10000,20000,3))

    async def test_new_goal_recovery_does_not_add_decisions_to_past_purchase_or_history_of_forecasts(self):
        draft={'understanding':{'evidenceNeeds':['sources']},'analyses':[{'tool':'sources'}]}
        for q in ('我上个月买过电脑吗？','统计上个月备注写着“预测”的收入来源。'):
            with self.subTest(q=q):
                p=await self.plan(q,draft,{})
                self.assertEqual([t.tool for t in p.analyses],['sources'])
