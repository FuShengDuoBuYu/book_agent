import unittest
from datetime import date
from decimal import Decimal

from app.tools.financial_analysis import ANALYSIS_TOOLS, run_analysis


TODAY = date(2026, 11, 19)


def row(when, amount, category, remark="", member="m1", name="春枝", bank="现金"):
    d = date.fromisoformat(when)
    return {"year": d.year, "month": d.month, "day": d.day, "money": amount,
            "costType": category, "orderRemark": remark, "memberKey": member,
            "memberName": name, "bankName": bank}


def period(label, start, end):
    return {"label": label, "startDate": start, "endDate": end}


class AggregateTests(unittest.TestCase):
    def run_aggregate(self, rows, metric="all", *, source_groups=None, **spec):
        self.assertIn("aggregate", ANALYSIS_TOOLS)
        return run_analysis("aggregate", rows, as_of=TODAY, metric=metric, aggregate=spec, source_groups=source_groups)

    def test_filtered_period_totals_and_changes_use_all_categories_before_display_limit(self):
        records = [row("2026-09-04", -220, "餐饮"), row("2026-09-05", -180, "展览"),
                   row("2026-10-02", -220, "餐饮"), row("2026-10-03", -440, "展览"),
                   row("2026-09-06", -1100, "房租"), row("2026-10-07", -180, "交通"),
                   row("2026-10-07", 3200, "工资")]
        spans = [period("九月", "2026-09-01", "2026-09-30"), period("十月", "2026-10-01", "2026-10-31")]
        result = self.run_aggregate(records, "expense", periods=spans, groupBy=["month", "category"],
                                    filters={"excludeCategories": ["房租", "交通"]}, limit=1)
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["totals"]["expense"], 1060)
        self.assertEqual([(p["period"], p["amount"], p["count"]) for p in result["periodTotals"]],
                         [("九月", 400, 2), ("十月", 660, 2)])
        change, = result["periodComparisons"]
        self.assertEqual((change["before"], change["after"], change["change"], change["changePercent"]),
                         (400, 660, 260, 65))
        self.assertNotIn("category", change)

    def test_period_totals_keep_filtered_zero_separate_from_missing_or_partial_coverage(self):
        records = [row("2026-09-02", 12, "工资"), row("2026-10-02", -10, "餐饮")]
        spans = [period("九月", "2026-09-01", "2026-09-30"), period("十月", "2026-10-01", "2026-10-31"),
                 period("八月", "2026-08-01", "2026-08-31"), period("七月", "2026-07-01", "2026-07-31")]
        result = run_analysis("aggregate", records, as_of=TODAY, metric="expense", aggregate={"periods": spans},
                              queried_intervals=[{"startDate": "2026-08-01", "endDate": "2026-10-15"}])
        self.assertEqual([(r["queryCoverage"], r["observed"], r["amount"]) for r in result["periodTotals"]],
                         [("complete", True, 0), ("partial", True, None), ("complete", False, None),
                          ("not_queried", False, None)])
        self.assertTrue(all(c["change"] is None for c in result["periodComparisons"]))
        self.assertEqual(result["coverage"][1]["totals"]["expense"], 10)

    def test_period_totals_double_membership_in_overlaps_but_union_counts_once(self):
        records = [row("2026-10-02", -20, "餐饮"), row("2026-10-03", 12, "工资")]
        spans = [period("两天", "2026-10-02", "2026-10-03"), period("单日", "2026-10-03", "2026-10-03")]
        result = self.run_aggregate(records, periods=spans, groupBy=["category"], limit=1)
        self.assertEqual([(p["net"], p["amount"], p["count"]) for p in result["periodTotals"]],
                         [(-8, None, 2), (12, None, 1)])
        self.assertEqual(result["totals"]["count"], 2)
        self.assertEqual({c["metric"]: c["change"] for c in result["periodComparisons"]},
                         {"net": 20, "income": 0, "expense": -20})

    def test_month_member_category_gross_ledger_totals_and_audited_exclusions(self):
        rows = [row("2026-10-01", 100, "转入"), row("2026-10-03", "-12.35", "餐饮", "面馆"),
                row("2026-10-07", "-3.40", "餐饮", "零食"), row("2026-10-09", -1, "交通"),
                row("2026-10-12", -25, "餐饮", "面馆", member="m2", name="云川")]
        result = self.run_aggregate(rows, groupBy=["month", "member", "category"],
                                    filters={"excludeCategories": ["交通"], "excludeRemarks": ["零食"]})
        self.assertEqual((result["totals"]["income"], result["totals"]["expense"], result["totals"]["net"]),
                         (100, 37.35, 62.65))
        self.assertEqual(result["totals"]["count"], 3)
        audit = result["filterAudit"]
        self.assertEqual((audit["scoped"]["count"], audit["excludedByFilters"]["count"], audit["included"]["count"]), (5, 2, 3))
        self.assertEqual(audit["excludedByFilters"]["expense"], 4.4)
        keyed = {(r["month"], r["memberKey"], r["category"]): r for r in result["rows"]}
        self.assertEqual(keyed["2026-10", "m1", "餐饮"]["expense"], 12.35)
        self.assertIsNone(keyed["2026-10", "m2", "餐饮"]["amount"])
        self.assertIsNone(keyed["2026-10", "m2", "餐饮"]["sharePercent"])

    def test_directional_stats_and_shares_use_the_whole_filtered_period_before_limit(self):
        rows = [row("2026-10-03", n, "票券") for n in (-10, -10, -70)]
        rows += [row("2026-10-03", -30, "零食"), row("2026-10-03", 500, "转入")]
        result = self.run_aggregate(rows, "expense", groupBy=["category"], limit=1)
        top = result["rows"][0]
        self.assertEqual((top["category"], top["amount"], top["incomeCount"], top["expenseCount"]), ("票券", 90, 0, 3))
        self.assertEqual((top["meanTransactionAmount"], top["medianTransactionAmount"], top["sharePercent"]), (30, 10, 75))
        self.assertEqual((result["totalGroupCount"], result["omittedGroupCount"], result["totals"]["expense"]), (2, 1, 120))
        self.assertEqual(result["filterAudit"]["excludedByMetric"]["income"], 500)

    def test_period_comparisons_distinguish_zero_recorded_group_from_unknown_period(self):
        rows = [row("2023-01-02", 90, "收入"), row("2024-01-01", -60, "交通"),
                row("2024-01-02", -20, "餐饮"), row("2026-10-02", -999, "交通")]
        spans = [period(str(year), f"{year}-01-01", f"{year}-01-31") for year in (2023, 2024, 2025)]
        result = self.run_aggregate(rows, "expense", periods=spans, groupBy=["category"])
        self.assertEqual(result["totals"]["expense"], 80)
        self.assertEqual([c["observed"] for c in result["coverage"]], [True, True, False])
        changes = {(c["afterPeriod"], c["category"]): c for c in result["comparisons"]}
        self.assertEqual((changes["2024", "交通"]["before"], changes["2024", "交通"]["after"],
                          changes["2024", "交通"]["change"], changes["2024", "交通"]["changePercent"]), (0, 60, 60, None))
        reverse = self.run_aggregate(rows, "expense", periods=list(reversed(spans)))
        self.assertIsNone(reverse["comparisons"][0]["before"])
        self.assertIsNone(reverse["comparisons"][0]["change"])

    def test_overlapping_periods_compare_signed_net_and_union_totals_do_not_double_count(self):
        rows = [row("2024-02-01", -100, "票券"), row("2024-02-02", 30, "转入")]
        result = self.run_aggregate(rows, "net", periods=[period("首日", "2024-02-01", "2024-02-01"),
                                                          period("两天", "2024-02-01", "2024-02-02")])
        change = result["comparisons"][0]
        self.assertEqual((change["before"], change["after"], change["change"], change["changePercent"]), (-100, -70, 30, 30))
        self.assertEqual(result["totals"]["net"], -70)
        self.assertEqual(result["totals"]["count"], 2)
        self.assertTrue(all(r["sharePercent"] is None and r["meanTransactionAmount"] is None for r in result["rows"]))

    def test_source_alias_and_member_filters_use_authorized_labels_and_exact_keys(self):
        rows = [row("2026-09-07", 10, "服务", "稿件初修"), row("2026-10-05", 20, "服务", "稿件精修"),
                row("2026-10-05", 40, "服务", "稿件精修", member="m2", name="云川"),
                row("2026-10-05", 30, "服务", "交付")]
        catalog = run_analysis("sources", rows, as_of=TODAY)["sources"]
        keys = [r["sourceKey"] for r in catalog if "稿件" in r["source"]]
        groups = [{"label": "编辑", "sourceKeys": keys}]
        filters = {"includeMembers": ["春枝"], "includeSources": ["编辑"],
                   "remarkContains": "稿件", "minAmount": 10, "maxAmount": 20}
        result = self.run_aggregate(rows, "income", groupBy=["source", "bank", "weekday", "year"],
                                    source_groups=groups, filters=filters)
        self.assertEqual(len(result["rows"]), 1)
        data = result["rows"][0]
        self.assertEqual((data["source"], data["amount"], data["year"], data["bank"]), ("编辑", 30, 2026, "现金"))
        self.assertEqual(data["weekday"], "星期一")
        household = self.run_aggregate(rows, "income", groupBy=["source"], source_groups=groups,
                                       filters={"includeSources": ["编辑"]})
        self.assertEqual([(r["source"], r["amount"]) for r in household["rows"]], [("编辑", 70)])
        grouped = run_analysis("sources", rows, as_of=TODAY, source_groups=groups)["sources"]
        key = next(r["sourceKey"] for r in grouped if r["memberKey"] == "m1" and r["source"] == "编辑")
        keyed = self.run_aggregate(rows, "income", source_groups=groups,
                                   filters={"includeSources": [key], "excludeMembers": ["m2"]})
        self.assertEqual(keyed["totals"]["income"], 30)

    def test_periods_outside_authorized_window_have_unknown_coverage_and_no_inverted_dates(self):
        rows = [row("2026-10-03", -20, "票券"), row("2026-09-07", -30, "票券")]
        spans = [period("早期", "2026-09-01", "2026-09-30"), period("近期", "2026-10-01", "2026-10-31")]
        result = run_analysis("aggregate", rows, as_of=TODAY, metric="all", aggregate={"periods": spans},
                              window_start=date(2026, 10, 1), window_end=date(2026, 10, 10))
        first, second = result["coverage"]
        self.assertEqual((first["startDate"], first["endDate"]), ("2026-09-01", "2026-09-30"))
        self.assertIsNone(first["effectiveStartDate"])
        self.assertIsNone(first["effectiveEndDate"])
        self.assertFalse(first["observed"])
        self.assertEqual(second["effectiveEndDate"], "2026-10-10")
        self.assertEqual(second["totals"]["expense"], 20)
        self.assertEqual({r["metric"] for r in result["comparisons"]}, {"income", "expense", "net"})
        self.assertTrue(all(r["before"] is None for r in result["comparisons"]))

    def test_requested_range_partly_queried_cannot_produce_a_period_comparison(self):
        rows = [row("2026-09-02", -900, "票券"), row("2026-10-05", -100, "票券"),
                row("2026-11-05", -120, "票券")]
        spans = [period("秋季", "2026-09-01", "2026-10-31"), period("十一月上旬", "2026-11-01", "2026-11-10")]
        actual = [{"startDate": "2026-10-01", "endDate": "2026-11-10"}]
        result = run_analysis("aggregate", rows, as_of=TODAY, metric="expense", aggregate={"periods": spans},
                              queried_intervals=actual)
        self.assertEqual(result["totals"]["expense"], 220)
        self.assertEqual(result["totalsBasis"], "queried_subset")
        first, second = result["coverage"]
        self.assertEqual([first["queryCoverage"], second["queryCoverage"]], ["partial", "complete"])
        self.assertEqual(first["queriedRanges"], [{"startDate": "2026-10-01", "endDate": "2026-10-31"}])
        self.assertEqual((first["effectiveStartDate"], first["effectiveEndDate"]), ("2026-10-01", "2026-10-31"))
        self.assertTrue(first["observed"])
        change = result["comparisons"][0]
        self.assertEqual((change["before"], change["after"], change["change"], change["changePercent"]), (None, 120, None, None))

    def test_disjoint_query_gaps_empty_queries_and_future_clips_are_explicit(self):
        spans = [period("九月", "2026-09-01", "2026-09-30"), period("十一月", "2026-11-01", "2026-11-30")]
        actual = [{"startDate": "2026-09-15", "endDate": "2026-09-30"},
                  {"startDate": "2026-09-01", "endDate": "2026-09-05"},
                  {"startDate": "2026-09-06", "endDate": "2026-09-07"},
                  {"startDate": "2026-11-01", "endDate": "2026-11-30"}]
        rows = [row("2026-09-02", -1, "票券"), row("2026-09-10", -200, "票券"), row("2026-11-18", -2, "票券")]
        def analyze(ranges):
            return run_analysis("aggregate", rows, as_of=TODAY, metric="expense", aggregate={"periods": spans},
                                queried_intervals=ranges)
        result = analyze(actual)
        self.assertEqual(result["totals"]["expense"], 3)
        first, second = result["coverage"]
        self.assertEqual(first["queriedRanges"], [{"startDate": "2026-09-01", "endDate": "2026-09-07"},
                                                  {"startDate": "2026-09-15", "endDate": "2026-09-30"}])
        self.assertEqual(first["queryCoverage"], "partial")
        self.assertEqual(second["effectiveEndDate"], TODAY.isoformat())
        self.assertEqual(second["queryCoverage"], "partial")
        empty = analyze([])
        self.assertEqual(empty["totals"]["count"], 0)
        for state in empty["coverage"]:
            self.assertEqual(state["queryCoverage"], "not_queried")
            self.assertEqual(state["queriedRanges"], [])
            self.assertIsNone(state["effectiveStartDate"])
            self.assertIsNone(state["effectiveEndDate"])

    def test_period_comparisons_roll_up_month_and_year_without_substituting_false_zeros(self):
        rows = [row("2025-09-02", -40, "票券"), row("2025-10-03", -60, "票券"),
                row("2026-09-08", -70, "票券"), row("2026-10-02", -50, "票券"),
                row("2026-10-02", -30, "餐饮", member="m2", name="云川")]
        spans = [period(str(year), f"{year}-09-01", f"{year}-10-31") for year in (2025, 2026)]
        result = self.run_aggregate(rows, "expense", periods=spans, groupBy=["year", "month", "category", "member"])
        self.assertEqual(result["dimensions"], ["period", "year", "month", "category", "member"])
        self.assertIn("comparisonDimensions", result)
        self.assertEqual(result["comparisonDimensions"], ["category", "member"])
        self.assertEqual(len(result["rows"]), 5)
        changes = {(row["category"], row["memberKey"]): row for row in result["comparisons"]}
        self.assertEqual(len(changes), 2)
        ticket = changes["票券", "m1"]
        self.assertEqual((ticket["before"], ticket["after"], ticket["change"], ticket["changePercent"]), (100, 120, 20, 20))
        self.assertNotIn("month", ticket)
        self.assertNotIn("year", ticket)
        self.assertEqual(changes["餐饮", "m2"]["change"], 30)

    def test_unknown_selectors_bad_ranges_and_duplicate_labels_raise(self):
        for spec in ({"groupBy": ["userId"]}, {"groupBy": ["month"] * 5}, {"sortBy": "arbitrary"},
                     {"filters": {"orderBy": "code"}}, {"filters": {"minAmount": 3, "maxAmount": 2}},
                     {"periods": [period("甲", "2026-01-10", "2026-01-01")]},
                     {"periods": [period("同名", "2026-01-02", "2026-01-02")] * 2}):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                self.run_aggregate([], **spec)

    def test_changes_distinguish_count_from_mean_and_reconcile_rounding(self):
        rows = [row("2026-09-01", n, "票券") for n in (-10, -11)]
        rows += [row("2026-10-01", n, "票券") for n in (-10, -12, -12)]
        rows += [row("2026-10-03", -40, "交通")]
        drivers = {r["name"]: r for r in run_analysis("changes", rows, as_of=TODAY)["expenseDrivers"]}
        ticket = drivers["票券"]
        self.assertIn("beforeCount", ticket)
        self.assertEqual((ticket["beforeCount"], ticket["afterCount"], ticket["meanAmountBefore"],
                          ticket["meanAmountAfter"]), (2, 3, 10.5, 11.33))
        self.assertEqual((ticket["countContribution"], ticket["meanContribution"]), (10.92, 2.08))
        self.assertEqual(Decimal(str(ticket["countContribution"])) + Decimal(str(ticket["meanContribution"])),
                         Decimal(str(ticket["change"])))
        self.assertIsNone(drivers["交通"]["countContribution"])
        self.assertIsNone(drivers["交通"]["meanAmountBefore"])
