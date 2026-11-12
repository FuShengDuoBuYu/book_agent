import unittest
from datetime import date

from app.tools.financial_analysis import run_analysis


def order(month, money, category="工资", remark="固定工资", day=5, year=2026):
    return {"year": year, "month": month, "day": day, "money": money,
            "costType": category, "orderRemark": remark, "clock": "12:00", "id": month * 100 + day}


class FinancialAnalysisTests(unittest.TestCase):
    def test_cashflow_reports_complete_month_average_and_source_share(self):
        self.orders += [order(10, -900, "医疗", "就诊"), order(11, -50, "餐饮", "早餐")]
        result = self.run_tool("cashflow")
        self.assertEqual(result["completeMonthAverage"]["expense"], 1500)
        self.assertEqual(result["completeMonthAverage"]["net"], 3500)
        self.assertEqual(result["incomeSources"][0]["sharePercent"], 100)
        self.assertEqual(result["incomeOrderCount"], 3)

    def setUp(self):
        self.today = date(2026, 11, 12)
        self.orders = [item for month in (8, 9, 10) for item in (
            order(month, 5000), order(month, -1000, "房租", "每月房租"),
            order(month, -200, "餐饮", "聚餐", day=10),
        )]

    def run_tool(self, name, **kwargs):
        return run_analysis(name, self.orders, as_of=self.today, **kwargs)

    def test_income_direction_matches_android_even_with_custom_category(self):
        self.orders = [order(10, 800, "其他", "兼职"), order(10, -60, "工资", "手续费")]
        result = self.run_tool("cashflow")
        self.assertEqual(result["incomeTotal"], 800)
        self.assertEqual(result["expenseTotal"], 60)
        self.assertEqual(result["netIncomeMinusExpense"], 740)

    def test_trend_keeps_partial_current_month_separate(self):
        self.orders += [order(11, 100)]
        result = self.run_tool("trends")
        self.assertTrue(result["months"][-1]["partial"])
        self.assertEqual(result["latestCompleteChange"]["to"], "2026-10")
        self.assertEqual(result["latestCompleteChange"]["incomeChange"], 0)

    def test_changes_attribute_extra_expenses_to_category(self):
        self.orders += [order(10, -700, "医疗", "看诊", day=12)]
        result = self.run_tool("changes")
        self.assertEqual(result["expenseChange"], 700)
        self.assertEqual(result["expenseDrivers"][0]["name"], "医疗")
        self.assertEqual(result["expenseDrivers"][0]["change"], 700)

    def test_forecast_separates_one_off_and_partial_month(self):
        self.orders += [order(10, 30000, "其他", "出售旧车"), order(11, 50000, "其他", "转入存款")]
        result = self.run_tool("forecast", metric="income", target_month="2026-12")
        self.assertEqual(result["estimate"], 5000)
        self.assertEqual(result["lower"], 5000)
        self.assertEqual(result["upper"], 5000)
        self.assertEqual(result["excludedIncomeTotal"], 30000)
        self.assertEqual(result["observedMonths"], ["2026-08", "2026-09", "2026-10"])

    def test_expense_and_net_forecasts_use_same_evidence(self):
        self.assertEqual(self.run_tool("forecast", metric="expense")["estimate"], 1200)
        self.assertEqual(self.run_tool("forecast", metric="net")["estimate"], 3800)

    def test_sparse_data_does_not_invent_precise_forecast(self):
        self.orders = [order(10, 3000)]
        result = self.run_tool("forecast", metric="income")
        self.assertFalse(result["sufficientData"])
        self.assertIsNone(result["estimate"])
        self.assertEqual(result["referenceValue"], 3000)

    def test_scenario_calculates_user_assumptions(self):
        result = self.run_tool("scenario", changes=[
            {"metric": "income", "amount": 200, "count": 8, "label": "新增八节课"},
            {"metric": "expense", "amount": -300, "count": 1, "label": "减少外食"},
        ])
        self.assertEqual(result["income"], 6600)
        self.assertEqual(result["expense"], 900)
        self.assertEqual(result["net"], 5700)
        self.assertEqual(len(result["assumptions"]), 2)

    def test_anomalies_identify_large_spend_without_calling_it_fraud(self):
        self.orders = [order(10, -20, "餐饮", "午餐", day=d) for d in range(1, 10)]
        self.orders.append(order(10, -900, "餐饮", "聚会", day=10))
        result = self.run_tool("anomalies")
        self.assertEqual(result["largeExpenses"][0]["amount"], 900)

    def test_recurring_income_and_expense_are_reported(self):
        result = self.run_tool("recurring")
        self.assertEqual(result["income"][0]["typicalAmount"], 5000)
        self.assertEqual(result["expense"][0]["typicalAmount"], 1000)

    def test_invalid_and_future_dates_do_not_pollute_forecast(self):
        self.orders += [order(12, 90000), order(0, 100000)]
        self.assertEqual(self.run_tool("forecast", metric="income")["estimate"], 5000)

    def test_joint_forecasts_have_consistent_net(self):
        self.orders = [order(month, income) for month, income in ((8, 100), (9, 200), (10, 300))]
        self.orders += [order(month, -expense, "消费", "日常") for month, expense in ((8, 300), (9, 100), (10, 200))]
        predicted_income = self.run_tool("forecast", metric="income")["estimate"]
        predicted_expense = self.run_tool("forecast", metric="expense")["estimate"]
        self.assertEqual(self.run_tool("forecast", metric="net")["estimate"], predicted_income - predicted_expense)

    def test_budget_uses_current_month_spend_and_remaining_days(self):
        self.orders += [order(11, -600, "购物", "日用品")]
        result = self.run_tool("budget", budget_amount=1500)
        self.assertEqual(result["spent"], 600)
        self.assertEqual(result["remaining"], 900)
        self.assertEqual(result["remainingDaysIncludingToday"], 19)

    def test_savings_goal_uses_user_balance_and_verified_cashflow(self):
        result = self.run_tool("savings", starting_balance=1000, target_amount=10000, months=3)
        self.assertEqual(result["remainingToTarget"], 9000)
        self.assertEqual(result["requiredMonthlySaving"], 3000)
        self.assertEqual(result["projectedBalance"], 12400)
        self.assertEqual(result["estimatedMonthsToTarget"], 3)

    def test_savings_goal_does_not_infer_account_balance(self):
        result = self.run_tool("savings", target_amount=10000)
        self.assertFalse(result["sufficientData"])
        self.assertIn("startingBalance", result["missingInputs"])

    def test_requested_partial_months_are_not_compared_as_complete_months(self):
        result = self.run_tool("trends", window_start=date(2026, 8, 15), window_end=date(2026, 10, 20))
        self.assertEqual([row["partial"] for row in result["months"]], [True, False, True])
        self.assertIsNone(result["latestCompleteChange"])

    def test_recurring_amount_excludes_unfinished_month(self):
        self.orders = [order(10, 5000), order(11, 500)]
        result = self.run_tool("recurring")["income"][0]
        self.assertEqual(result["typicalAmount"], 5000)
        self.assertEqual(result["partialMonths"], ["2026-11"])


if __name__ == "__main__":
    unittest.main()
