import unittest
from datetime import datetime

from app.agent.planner import QueryPlanner
from app.schemas.plan import AgentPlan


class AnalysisPlannerTests(unittest.TestCase):
    def test_unused_null_collections_and_source_are_normalized(self):
        plan = AgentPlan.model_validate({'analyses': [{'tool': 'optimization', 'protectedCategories': None},
                 {'tool': 'scenario', 'changes': [{'metric': 'income', 'amount': 200, 'source': None}]}]})
        self.assertEqual(plan.analyses[0].protectedCategories, [])
        self.assertEqual(plan.analyses[1].changes[0].source, '')
    def test_past_history_reference_does_not_override_forecast_target(self):
        plan = AgentPlan.model_validate({"analyses": [{"tool": "forecast", "targetMonth": "2026-12"}]})
        normalized = QueryPlanner.__new__(QueryPlanner)._normalize_plan(plan, datetime(2026, 11, 12), "根据上个月的收入，预测未来一个月收入")
        self.assertEqual(normalized.analyses[0].targetMonth, "2026-12")

    def test_savings_target_does_not_imply_starting_balance(self):
        plan = AgentPlan.model_validate({"analyses": [{"tool": "savings", "targetAmount": 20000}]})
        normalized = QueryPlanner.__new__(QueryPlanner)._normalize_plan(plan, datetime(2026, 11, 12), "存款目标20000元，多久能实现？")
        self.assertIsNone(normalized.analyses[0].startingBalance)

    def test_quantified_budget_and_savings_inputs_cannot_be_dropped(self):
        for tool, question in (("budget", "本月预算4000元"), ("savings", "已有存款10000元，3个月存到20000元")):
            with self.subTest(tool=tool), self.assertRaises(ValueError):
                plan = AgentPlan.model_validate({"analyses": [{"tool": tool}]})
                QueryPlanner.__new__(QueryPlanner)._normalize_plan(plan, datetime(2026, 11, 12), question)

    def test_quantified_scenario_cannot_drop_user_assumptions(self):
        plan = AgentPlan.model_validate({"analyses": [{"tool": "scenario"}]})
        with self.assertRaisesRegex(ValueError, "scenario.changes"):
            QueryPlanner.__new__(QueryPlanner)._normalize_plan(plan, datetime(2026, 11, 12), "增加8节课每节200元，减少300元支出")

    def test_unused_metric_null_does_not_reject_anomaly_analysis(self):
        plan = AgentPlan.model_validate({"analyses": [{"tool": "anomalies", "metric": None}]})
        self.assertEqual(plan.analyses[0].tool, "anomalies")

    def test_explicit_relative_target_is_resolved_by_calendar(self):
        plan = AgentPlan.model_validate({"analyses": [{"tool": "forecast", "targetMonth": "2026-12"}]})
        normalized = QueryPlanner.__new__(QueryPlanner)._normalize_plan(plan, datetime(2026, 12, 12), "预测下个月的收支")
        self.assertEqual(normalized.analyses[0].targetMonth, "2027-01")

    def test_model_date_window_aliases_keep_exact_range(self):
        plan = AgentPlan.model_validate({"analysisWindow": {"start": "2026-05-01", "end": "2026-11-12"},
                                         "analyses": [{"tool": "trends"}]})
        self.assertEqual(plan.analysisWindow.startDate.isoformat(), "2026-05-01")
        self.assertEqual(plan.analysisWindow.endDate.isoformat(), "2026-11-12")

    def test_duplicate_query_id_cannot_bind_different_filters(self):
        plan = AgentPlan.model_validate({"toolCalls": [
            {"id": "ledger", "toolName": "search_personal_orders", "args": {"cost_type": "餐饮"}},
            {"id": "ledger", "toolName": "search_personal_orders", "args": {"cost_type": "不限"}},
        ], "analyses": [{"tool": "budget", "budgetAmount": 500, "queryIds": ["ledger"]}]})
        with self.assertRaisesRegex(ValueError, "ambiguous query id"):
            QueryPlanner.__new__(QueryPlanner)._normalize_plan(plan, datetime(2026, 11, 12), "餐饮预算")

    def test_known_tool_analysis_type_is_normalized(self):
        plan = AgentPlan.model_validate({"analysisType": "scenario", "analyses": [{"tool": "scenario"}]})
        self.assertEqual(plan.analysisType, "financial_analysis")
        self.assertEqual(plan.analyses[0].tool, "scenario")

    def test_forecast_reads_history_across_year_boundary(self):
        plan = AgentPlan.model_validate({
            "analysisType": "financial_analysis", "analyses": [{"tool": "forecast", "metric": "expense"}],
            "toolCalls": [{"toolName": "search_personal_orders", "args": {"year": 2027, "month": 2}}],
        })
        normalized = QueryPlanner.__new__(QueryPlanner)._normalize_plan(plan, datetime(2027, 1, 12), "预测下个月开销")
        periods = {(call.args.year, call.args.month) for call in normalized.toolCalls}
        self.assertEqual(periods, {(2026, month) for month in range(7, 13)} | {(2027, 1)})
        self.assertEqual(normalized.analyses[0].targetMonth, "2027-02")

    def test_analysis_tools_are_composable_for_broad_questions(self):
        plan = AgentPlan.model_validate({"analysisType": "financial_analysis", "analyses": [
            {"tool": "trends"}, {"tool": "changes"}, {"tool": "anomalies"}, {"tool": "recurring"},
        ]})
        normalized = QueryPlanner.__new__(QueryPlanner)._normalize_plan(plan, datetime(2026, 11, 12), "分析我的财务情况", "家庭版")
        self.assertTrue(normalized.needsTools)
        self.assertEqual({c.toolName for c in normalized.toolCalls}, {"search_family_orders"})
        self.assertEqual(len(normalized.analyses), 4)

    def test_scenario_preserves_explicit_assumptions(self):
        plan = AgentPlan.model_validate({"analyses": [{"tool": "scenario", "changes": [
            {"metric": "expense", "amount": -300, "count": 1, "label": "减少外食"},
        ]}]})
        normalized = QueryPlanner.__new__(QueryPlanner)._normalize_plan(plan, datetime(2026, 11, 12), "少花300会怎么样")
        self.assertEqual(normalized.analyses[0].changes[0].amount, -300)
        self.assertEqual(len(normalized.toolCalls), 7)

    def test_explicit_range_expands_every_month(self):
        plan = AgentPlan.model_validate({"analysisType": "financial_analysis", "analyses": [{"tool": "trends"}],
            "analysisWindow": {"startDate": "2026-08-15", "endDate": "2026-10-20"},
            "toolCalls": [{"toolName": "search_personal_orders", "args": {"year": 2026, "month": 8}}]})
        normalized = QueryPlanner.__new__(QueryPlanner)._normalize_plan(plan, datetime(2026, 11, 12), "分析8月15到10月20日")
        self.assertEqual([call.args.month for call in normalized.toolCalls], [8, 9, 10])

    def test_forecast_keeps_requested_analysis_window(self):
        plan = AgentPlan.model_validate({"analysisWindow": {"startDate": "2026-09-01", "endDate": "2026-09-30"},
            "analyses": [{"tool": "cashflow"}, {"tool": "forecast"}]})
        normalized = QueryPlanner.__new__(QueryPlanner)._normalize_plan(plan, datetime(2026, 11, 12), "分析9月并预测下月")
        self.assertEqual(normalized.analysisWindow.startDate.isoformat(), "2026-09-01")
        self.assertEqual([call.args.month for call in normalized.toolCalls], list(range(5, 12)))

    def test_deduplicated_queries_keep_all_analysis_bindings(self):
        plan = AgentPlan.model_validate({"toolCalls": [
            {"id": "all_a", "toolName": "search_personal_orders", "args": {"cost_type": "不限"}},
            {"id": "all_b", "toolName": "search_personal_orders", "args": {"cost_type": "全部"}},
        ], "analyses": [{"tool": "forecast", "queryIds": ["all_b"]}]})
        normalized = QueryPlanner.__new__(QueryPlanner)._normalize_plan(plan, datetime(2026, 11, 12), "预测下月")
        self.assertEqual(len(normalized.toolCalls), 7)
        self.assertTrue(all("all_b" in call.sourceIds for call in normalized.toolCalls))
