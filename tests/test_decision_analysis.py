import unittest
from datetime import date

from app.schemas.plan import ScenarioChange
from app.tools.financial_analysis import ANALYSIS_TOOLS, run_analysis


def ledger():
    return [{"year": 2026, "month": month, "day": day, "money": amount,
             "costType": category, "orderRemark": remark}
            for month in range(5, 11)
            for day, amount, category, remark in (
                (2, 5000, "其他", "固定工资"), (3, 1600, "收入", "家教课时费"),
                (4, -1800, "房租", "月租"), (5, -600, "餐饮", "日常餐饮"), (6, -200, "交通", "公交"))]


class DecisionAnalysisTests(unittest.TestCase):
    def tool(self, name, records=None, **kwargs):
        self.assertIn(name, ANALYSIS_TOOLS, "The analytical capability must be registered")
        return run_analysis(name, records or ledger(), as_of=date(2026, 11, 12), **kwargs)

    def test_projection_calculates_total_without_inventing_starting_balance(self):
        result = self.tool("projection", months=3)
        self.assertEqual(result["totalNet"], 12000)
        self.assertEqual(result["endMonth"], "2027-02")
        self.assertIsNone(result["projectedBalance"])

    def test_source_percentage_applies_only_to_tutoring(self):
        change = ScenarioChange(metric="income", percent=-50, source="家教", label="家教少一半")
        result = self.tool("projection", months=3, changes=[change.model_dump()])
        self.assertEqual(result["monthlyIncome"], 5800)
        self.assertEqual(result["totalNet"], 9600)
        longer = self.tool("projection", months=5, changes=[change.model_dump()])
        self.assertEqual(longer["totalNet"], 16000)

    def test_unknown_source_cannot_be_replaced_by_total_income(self):
        result = self.tool("projection", months=3, changes=[{"metric": "income", "percent": -50, "source": "稿费"}])
        self.assertFalse(result["sufficientData"])
        self.assertIn("source", result["missingInputs"])

    def test_runway_requires_liquid_balance_and_reduction_assumption(self):
        result = self.tool("runway")
        self.assertFalse(result["sufficientData"])
        self.assertIn("startingBalance", result["missingInputs"])
        self.assertNotIn("runwayMonths", result)

    def test_runway_uses_balance_not_monthly_net(self):
        result = self.tool("runway", starting_balance=10000, changes=[{"metric": "income", "percent": -100}])
        self.assertAlmostEqual(result["runwayMonths"], 3.85)
        self.assertEqual(result["monthlyDeficit"], 2600)

    def test_purchase_does_not_infer_affordability_without_balance(self):
        result = self.tool("purchase", purchase_amount=6000)
        self.assertFalse(result["sufficientData"])
        self.assertIn("startingBalance", result["missingInputs"])
        self.assertIsNone(result.get("affordableUnderAssumptions"))

    def test_purchase_shows_post_purchase_cash_and_reserve(self):
        result = self.tool("purchase", purchase_amount=6000, starting_balance=12000, reserve_amount=5000)
        self.assertEqual(result["cashAfterPurchase"], 6000)
        self.assertEqual(result["reserveMargin"], 1000)

    def test_optimization_does_not_reduce_protected_or_essential_expenses(self):
        rows = ledger() + [{"year": 2026, "month": month, "day": 7, "money": -300,
                           "costType": "购物", "orderRemark": "购物"} for month in range(5, 11)]
        result = self.tool("optimization", rows, monthly_improvement=1000, protected_categories=["房租", "餐饮"])
        self.assertEqual(result["reviewableMonthlySpend"], 300)
        self.assertEqual(result["gapBeyondReviewableSpend"], 700)
        self.assertEqual([row["category"] for row in result["candidates"]], ["购物"])

    def test_income_stability_measures_observed_history_only(self):
        result = self.tool("income_stability")
        self.assertEqual(result["incomeMin"], 6600)
        self.assertEqual(result["incomeMax"], 6600)
        self.assertEqual(result["sources"][0]["sharePercent"], 75.76)

    def test_goal_optimization_uses_additional_gap_not_total_required_saving(self):
        result = self.tool('optimization', starting_balance=10000, target_amount=20000, months=3,
                           monthly_improvement=3333.33)
        self.assertEqual(result['monthlyImprovement'], 0)
        self.assertEqual(result['goalMonthlyRequirement'], 3333.33)
        self.assertEqual(result['baselineMonthlySaving'], 4000)

    def test_protected_expenses_in_remarks_are_excluded(self):
        rows = ledger() + [{"year": 2026, "month": month, "day": 7, "money": -500,
                           "costType": "其他", "orderRemark": "房租补缴"} for month in range(5, 11)]
        result = self.tool("optimization", rows, monthly_improvement=1000)
        self.assertEqual(result["reviewableMonthlySpend"], 0)

    def test_quality_flags_nonrecurring_inflows_without_calling_them_errors(self):
        rows = ledger() + [{"year": 2026, "month": 10, "day": 10, "money": 30000,
                           "costType": "其他", "orderRemark": "出售旧车"}]
        result = self.tool("data_quality", rows)
        self.assertEqual(result["nonrecurringIncomeTotal"], 30000)
        self.assertEqual(result["nonrecurringIncomeCount"], 1)
        self.assertIn("核查", result["interpretation"])
