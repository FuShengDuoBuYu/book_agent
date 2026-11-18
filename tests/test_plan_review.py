import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.schemas.plan import AgentPlan


TODAY = date(2026, 11, 19)


def decision(analyses, *, adequate=False, window=None):
    return {"analyses": analyses, "analysisWindow": window, "answerInstruction": "用核算结果解释结果和限制",
            "adequate": adequate, "issues": [] if adequate else ["现有步骤与目标不匹配"]}


def reply(data):
    return SimpleNamespace(content=json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else data,
                           response_metadata={"prompt_eval_count": 30, "eval_count": 15, "done_reason": "stop",
                                              "unrelated": "private-debug-content"})


class PlanReviewTests(unittest.IsolatedAsyncioTestCase):
    async def review(self, plan, question, responses, prior=None):
        from app.agent.plan_review import PlanReviewer
        model = AsyncMock()
        model.ainvoke.side_effect = [reply(data) if not isinstance(data, Exception) else data for data in responses]
        with patch("app.agent.plan_review.get_structured_model", return_value=model):
            result, audit = await PlanReviewer().review(question, "用户：刚才在核对上期条件", plan, TODAY, "个人版", prior or {})
        self.assertNotIn("private-debug-content", json.dumps(audit))
        return result, audit, model

    async def test_future_income_replaces_history_only_plan_and_keeps_original_unmodified(self):
        plan = AgentPlan.model_validate({"understanding": {"objective": "预测2027年1月个人收入",
            "requestedAnswers": ["一月预计收入和依据"]}, "analyses": [{"tool": "trends"}],
            "toolCalls": [{"id": "ledger", "toolName": "search_personal_orders"}]})
        fixed = decision([{"tool": "forecast", "queryIds": ["ledger"], "metric": "income", "targetMonth": "2027-01"}])
        actual, audit, model = await self.review(plan, "预计2027年1月我能有多少收入？", [fixed])
        self.assertEqual((actual.analyses[0].tool, actual.analyses[0].metric, actual.analyses[0].targetMonth),
                         ("forecast", "income", "2027-01"))
        self.assertEqual(plan.analyses[0].tool, "trends")
        self.assertEqual(audit["status"], "reviewed")
        self.assertEqual(model.ainvoke.await_count, 1)
        prompts = model.ainvoke.await_args.args[0]
        data = json.loads(prompts[-1][1])
        self.assertEqual(data["plan"]["understanding"]["requestedAnswers"], ["一月预计收入和依据"])
        self.assertEqual(data["logicalQueryIds"], ["ledger"])
        self.assertIn("projection", prompts[0][1])
        self.assertIn("2026-11-19", json.dumps(data))

    async def test_aggregate_filters_corrected_and_dates_json_safe_in_model_input_and_audit(self):
        grouped = {"tool": "aggregate", "metric": "expense", "aggregate": {"groupBy": ["member"]}}
        plan = AgentPlan.model_validate({"analyses": [grouped], "analysisWindow":
                                     {"startDate": "2026-09-01", "endDate": "2026-10-31"}})
        corrected = {**grouped, "aggregate": {"groupBy": ["member"], "filters": {"excludeCategories": ["房租"]},
                             "periods": [{"label": "九月", "startDate": "2026-09-01", "endDate": "2026-09-30"}]}}
        prior = {"scope": [["search_personal_orders", "不限", ""]], "startingBalance": 100,
                 "referenceDate": date(2026, 9, 1)}
        actual, audit, model = await self.review(plan, "按人统计排除房租后的支出。",
                                               [decision([corrected], window=plan.analysisWindow.model_dump(mode="json"))], prior)
        self.assertEqual(actual.analyses[0].aggregate.filters.excludeCategories, ["房租"])
        payload = json.loads(model.ainvoke.await_args.args[0][-1][1])
        self.assertEqual(payload["previousContext"]["referenceDate"], "2026-09-01")
        self.assertEqual(payload["previousContext"]["scope"], prior["scope"])
        self.assertIn("2026-09-30", json.dumps(audit))

    async def test_unknown_query_binding_never_reaches_executor(self):
        plan = AgentPlan.model_validate({"toolCalls": [{"id": "mine", "toolName": "search_personal_orders"}]})
        bad = decision([{"tool": "aggregate", "queryIds": ["foreign"]}])
        result, audit, _ = await self.review(plan, "按我的账本分组", [bad, bad])
        self.assertIsNone(result)
        self.assertEqual(audit["status"], "unavailable")
        self.assertNotIn("decision", audit)

    async def test_correct_complete_plan_may_keep_default_logical_query(self):
        task = {"tool": "purchase", "purchaseAmount": 700}
        plan = AgentPlan.model_validate({"analyses": [task]})
        result, audit, _ = await self.review(plan, "用700元买扫描仪可行吗？我没说余额。", [decision([task], adequate=True)])
        self.assertTrue(result.adequate)
        self.assertEqual(result.analyses, plan.analyses)
        self.assertIsNone(result.analyses[0].startingBalance)
        self.assertEqual(audit["decision"]["analyses"][0]["queryIds"], [])

    async def test_retry_once_only_for_malformed_structure_not_network_timeout(self):
        plan = AgentPlan()
        corrected = decision([{"tool": "cashflow"}])
        result, audit, model = await self.review(plan, "看看收支", ["这是一段未按协议输出的文本", corrected])
        self.assertIsNotNone(result)
        self.assertEqual(model.ainvoke.await_count, 2)
        self.assertEqual(len(audit["attempts"]), 2)
        result, audit, model = await self.review(plan, "看看收支", [TimeoutError("sensitive-provider-detail")])
        self.assertIsNone(result)
        self.assertEqual(model.ainvoke.await_count, 1)
        self.assertNotIn("sensitive-provider-detail", json.dumps(audit))

    async def test_extra_output_fields_and_missing_required_fields_fail_closed(self):
        for bad in ({**decision([{"tool": "cashflow"}]), "toolCalls": [{"toolName": "search_family_orders"}]},
                    {"analyses": [{"tool": "cashflow"}], "adequate": True}):
            with self.subTest(bad=bad):
                result, audit, model = await self.review(AgentPlan(), "只查我的收支", [bad, bad])
                self.assertIsNone(result)
                self.assertEqual(model.ainvoke.await_count, 2)
                self.assertEqual(audit["status"], "unavailable")

    async def test_new_balance_without_user_or_scoped_condition_is_rejected(self):
        initial = AgentPlan.model_validate({"analyses": [{"tool": "purchase", "purchaseAmount": 700}]})
        unsupported = decision([{"tool": "purchase", "purchaseAmount": 700, "startingBalance": 9999}])
        result, audit, _ = await self.review(initial, "用七百元买扫描仪可行吗？", [unsupported, unsupported])
        self.assertIsNone(result)
        self.assertEqual(audit["status"], "unavailable")


    async def test_prior_balance_requires_matching_scope_and_an_actual_followup(self):
        bare = {"tool": "purchase", "purchaseAmount": 700}
        inherited = decision([{**bare, "startingBalance": 10000}])
        plan = AgentPlan.model_validate({"analyses": [bare], "followUpOfPreviousQuestion": True})
        prior = {"scope": [["search_personal_orders", "不限", ""]], "startingBalance": 10000}
        result, _, _ = await self.review(plan, "按刚才条件买七百元的扫描仪可以吗？", [inherited], prior)
        self.assertEqual(result.analyses[0].startingBalance, 10000)
        family_prior = {**prior, "scope": [["search_family_orders", "不限", ""]]}
        result, audit, _ = await self.review(plan, "按刚才条件买七百元的扫描仪可以吗？", [inherited], family_prior)
        self.assertIsNone(result)
        self.assertEqual(audit["error"], "unsupported_startingBalance")

    async def test_explicit_chinese_conditions_allow_review_without_guessing_unrelated_balances(self):
        initial = AgentPlan(analyses=[{"tool": "savings"}])
        corrected = decision([{"tool": "savings", "startingBalance": 10000, "targetAmount": 12000, "months": 3}])
        result, _, _ = await self.review(initial, "我手头有一万，想三个月内存到一万二千元。", [corrected])
        self.assertEqual(result.analyses[0].targetAmount, 12000)
        result, audit, _ = await self.review(initial, "我想花一万元，然后估算储蓄。", [corrected])
        self.assertIsNone(result)
        self.assertEqual(audit["error"], "unsupported_startingBalance")

    async def test_second_next_month_cannot_be_silently_shortened_by_one_month(self):
        initial = AgentPlan(analyses=[{"tool": "trends"}])
        wrong = decision([{"tool": "forecast", "metric": "income", "targetMonth": "2026-12"}])
        result, audit, _ = await self.review(initial, "预计下下个月收入多少？", [wrong])
        self.assertIsNone(result)
        self.assertEqual(audit["error"], "unsupported_targetMonth")


    async def test_default_ledger_and_projection_start_do_not_require_invented_history(self):
        task = {"tool": "projection", "months": 3, "targetMonth": "2026-12", "queryIds": ["ledger"]}
        result, _, model = await self.review(AgentPlan(), "按现状预计未来三个月结余。", [decision([task])])
        self.assertIsNotNone(result)
        self.assertEqual(json.loads(model.ainvoke.await_args.args[0][-1][1])["logicalQueryIds"], ["ledger"])

    async def test_existing_tasks_cannot_move_from_personal_to_authorized_family_scope(self):
        plan = AgentPlan.model_validate({"toolCalls": [{"id": "mine", "toolName": "search_personal_orders"},
            {"id": "home", "toolName": "search_family_orders"}], "analyses": [{"tool": "purchase", "queryIds": ["mine"]}]})
        for task in ({"tool": "purchase", "queryIds": ["home"]}, {"tool": "projection", "queryIds": []}):
            with self.subTest(task=task):
                result, audit, _ = await self.review(plan, "继续核对我的决策", [decision([task])])
                self.assertIsNone(result)
                self.assertIn("scope", audit["error"])

    async def test_dates_from_history_do_not_supply_forecast_target_or_projection_length(self):
        question = "用2026年10月和过去六个月的记录，预测2027年1月以及从那时起未来三个月的结余。"
        for task in ({"tool": "forecast", "targetMonth": "2026-10"},
                     {"tool": "projection", "targetMonth": "2027-01", "months": 6},
                     {"tool": "forecast", "targetMonth": "2026-12"}):
            with self.subTest(task=task):
                result, _, _ = await self.review(AgentPlan(), question, [decision([task])])
                self.assertIsNone(result)
        correct, _, _ = await self.review(AgentPlan(), question,
            [decision([{"tool": "projection", "targetMonth": "2027-01", "months": 3}])])
        self.assertIsNotNone(correct)

    async def test_reviewer_cannot_multiply_stated_price_or_replace_a_goal_by_an_assumed_cut(self):
        tasks = [("花700元买扫描仪", {"tool": "purchase", "purchaseAmount": 7000}),
                 ("想每月多留三百", {"tool": "optimization", "monthlyImprovement": 300,
                                 "changes": [{"metric": "expense", "source": "餐饮", "amount": -300}]})]
        for question, task in tasks:
            with self.subTest(question=question):
                result, audit, _ = await self.review(AgentPlan(), question, [decision([task])])
                self.assertIsNone(result)
                self.assertTrue(audit["error"].startswith("unsupported_"))


class ConditionSanitizerTests(unittest.TestCase):
    def sanitize(self, question, task, previous=None, *, follow=False):
        from app.agent.plan_review import sanitize_user_conditions
        plan = AgentPlan.model_validate({"analyses": [task], "followUpOfPreviousQuestion": follow})
        issues = sanitize_user_conditions(plan, question, "个人版", previous or {})
        json.dumps(issues)
        return plan.analyses[0], issues

    def test_roles_and_negation_never_turn_a_negative_statement_or_a_price_into_a_balance(self):
        result, issues = self.sanitize("我没有一万存款，花七百元买扫描仪可行吗？",
                                    {"tool": "purchase", "startingBalance": 10000, "purchaseAmount": 700, "reserveAmount": 700})
        self.assertIsNone(result.startingBalance)
        self.assertIsNone(result.reserveAmount)
        self.assertEqual(result.purchaseAmount, 700)
        self.assertEqual({i["field"] for i in issues}, {"startingBalance", "reserveAmount"})
        result, _ = self.sanitize("我没有一万存款，现在有三百元。",
                                {"tool": "purchase", "startingBalance": 10000},
                                {"scope": [["search_personal_orders", "不限", ""]], "startingBalance": 10000}, follow=True)
        self.assertIsNone(result.startingBalance)

    def test_real_acceptance_phrases_keep_the_typed_amount_for_its_own_role(self):
        examples = [
            ("我现在可动用12000元，买6000元电脑后还要留5000元应急钱，是否足够？",
             {"tool": "purchase", "startingBalance": 12000, "purchaseAmount": 6000, "reserveAmount": 5000}),
            ("假设现在有10000元，想三个月存到20000元", {"tool": "savings", "startingBalance": 10000, "targetAmount": 20000}),
            ("本月餐饮预算四千元", {"tool": "budget", "budgetAmount": 4000}),
            ("我想每月多留下三百", {"tool": "optimization", "monthlyImprovement": 300}),
        ]
        for question, original in examples:
            with self.subTest(question=question):
                task, issues = self.sanitize(question, original)
                self.assertEqual(issues, [])
                for field, value in original.items():
                    self.assertEqual(getattr(task, field), value)

    def test_naked_saving_goal_drops_made_up_cuts_but_preserves_scoped_prior_or_explicit_changes(self):
        cut = {"metric": "expense", "source": "餐饮", "amount": -300}
        task = {"tool": "optimization", "changes": [cut], "monthlyImprovement": 300}
        cleaned, issues = self.sanitize("希望每月多留300元。", task)
        self.assertFalse(cleaned.changes)
        self.assertEqual(cleaned.monthlyImprovement, 300)
        self.assertEqual(issues[0]["field"], "changes")
        prior = {"scope": [["search_personal_orders", "不限", ""]], "changes": [cut]}
        inherited, _ = self.sanitize("沿用刚才的假设，我希望每月多留300元。", task, prior, follow=True)
        self.assertEqual(inherited.changes[0].amount, -300)
        half, _ = self.sanitize("如果家教少一半呢？", {"tool": "scenario", "changes": [
                                {"metric": "income", "source": "家教", "percent": -50}]})
        self.assertEqual(half.changes[0].percent, -50)


    def test_explicit_per_lesson_and_expense_assumptions_keep_source_direction_and_count(self):
        question = "如果下个月家教增加8节课，每节200元，并减少300元餐饮支出，结余会怎样？"
        income = {"metric": "income", "source": "家教", "amount": 200, "count": 8}
        expense = {"metric": "expense", "source": "餐饮", "amount": -300}
        task, issues = self.sanitize(question, {"tool": "scenario", "changes": [income, expense]})
        self.assertEqual(len(task.changes), 2)
        self.assertEqual(issues, [])
        altered, _ = self.sanitize(question, {"tool": "scenario", "changes": [
            {**expense, "amount": 300}, {**income, "source": "项目X"}, {**income, "count": 3}]})
        self.assertEqual(altered.changes, [])

    def test_amount_proof_is_not_transferred_between_purchases_and_reserves(self):
        result, _ = self.sanitize("买6000元电脑后还要留5000元应急钱。", {"tool": "purchase",
                                  "startingBalance": 6000, "purchaseAmount": 5000, "reserveAmount": 6000})
        self.assertIsNone(result.startingBalance)
        self.assertIsNone(result.purchaseAmount)
        self.assertIsNone(result.reserveAmount)


    def test_source_only_turn_flags_retracted_prior_balance_even_when_task_has_no_balance(self):
        prior = {"scope": [["search_personal_orders", "不限", ""]], "startingBalance": 10000, "reserveAmount": 500}
        _, issues = self.sanitize("我没有一万存款，先列来源。", {"tool": "sources"}, prior, follow=True)
        self.assertEqual({issue["field"] for issue in issues}, {"startingBalance"})
        self.assertTrue(issues[0]["retirePrevious"])
        # The planner uses this explicit rejection to clear its canonical stored condition.
        revised = {k: v for k, v in prior.items() if k not in {issue["field"] for issue in issues}}
        task, _ = self.sanitize("按刚才再算买价700元的扫描仪", {"tool": "purchase", "startingBalance": 10000,
                                "reserveAmount": 500}, revised, follow=True)
        self.assertIsNone(task.startingBalance)
        self.assertEqual(task.reserveAmount, 500)
        _, issues = self.sanitize("先列来源。", {"tool": "sources"}, prior, follow=True)
        self.assertEqual(issues, [])


    def test_unsupported_new_balance_alone_does_not_retire_valid_canonical_balance(self):
        prior = {"scope": [["search_personal_orders", "不限", ""]], "startingBalance": 10000}
        task, issues = self.sanitize("沿用已说的条件，能买七百元的扫描仪吗？",
                                    {"tool": "purchase", "purchaseAmount": 700, "startingBalance": 9999}, prior, follow=True)
        self.assertIsNone(task.startingBalance)
        self.assertFalse(any(i.get("retirePrevious") for i in issues))
