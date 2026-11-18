import unittest

from app.agent.report_values import numerical_issues, quantity


class CalendarProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.facts = [
            {"id": "f1", "values": [quantity("参考月份", month, "month") for month in
                                   ("2026-08", "2026-09", "2026-10", "2027-01")]
                                  + [quantity("总额", 3700)]},
            {"id": "f2", "calendars": ["2025-08", "2025-09", "2025-10"],
             "values": [quantity("分项", 500)]},
        ]

    def issues(self, text, ids=("f1",)):
        return numerical_issues(text, ids, self.facts)

    def test_enumeration_and_duration_are_supported_by_the_same_months(self):
        self.assertEqual(self.issues("参考2026 年 8 月、9 月、10 月这三个月。"), [])
        self.assertEqual(self.issues("参考2026年8、9、10月份这3个月。"), [])
        self.assertTrue(self.issues("参考2026年8月、9月、10月这四个月。"))
        self.assertTrue(self.issues("参考2026年8月、8月、8月这三个月。"))

    def test_unqualified_range_needs_a_concrete_year_in_cited_evidence(self):
        self.assertEqual(self.issues("依据历史记录（8-10 月）。"), [])
        self.assertTrue(self.issues("依据历史记录（8-10 月）。", ("f1", "f2")))
        self.assertEqual(self.issues("2025年8月的记录，参照（8-10 月）。", ("f1", "f2")), [])
        self.assertTrue(self.issues("依据历史记录（8-11月）。"))

    def test_ranges_require_each_intervening_month_in_the_cited_facts(self):
        gapped = [{"id": "g", "values": [quantity("参考月", "2026-08", "month"), quantity("参考月", "2026-10", "month")]},
                  {"id": "other", "calendars": ["2026-09"]}]
        for text in ("2026年8月至10月", "2026年8月至2026年10月", "（8-10 月）"):
            with self.subTest(text=text):
                self.assertTrue(numerical_issues(text, ["g"], gapped))
        self.assertTrue(self.issues("2025年8月、9月、10月", ("f1",)))
        self.assertTrue(self.issues("2025-08月份", ("f1",)))

    def test_month_suffix_and_ordinal_headings_preserve_strict_numeric_checks(self):
        for heading in ("1、参考月份", "（2）计算方法", "### 3. 方法"):
            with self.subTest(heading=heading):
                self.assertEqual(self.issues(heading + "\n采用2026-10月份的记录。"), [])
        for prose in ("预计金额为2026-10元。", "收入8～10元。", "8-10个月。", "（8-10月）赚了999元。",
                      "基准为3.50元。", "3.元。", "8-10月元。", "2026年8月、9月、13月。"):
            with self.subTest(prose=prose):
                self.assertTrue(self.issues(prose))

    def test_day_precision_is_not_reinterpreted_as_a_month_list(self):
        facts = [{"id": "f", "calendars": ["2026-08-01", "2026-10-15"]}]
        self.assertEqual(numerical_issues("日期2026-08-01至2026-10-15。", ["f"], facts), [])
        self.assertTrue(numerical_issues("日期2026-08-01至2026-10-16。", ["f"], facts))

    def test_repeating_total_still_requires_its_own_fact(self):
        self.assertEqual(self.issues("项目收入总额为3,700元。"), [])
        self.assertTrue(self.issues("1、按分项分解\n总额3,700元。", ("f2",)))
