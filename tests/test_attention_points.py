import unittest
from datetime import date

from app.agent.analysis_findings import build_analysis_findings
from app.agent.attention_points import build_attention_points, requested_attention_count
from app.agent.evidence_report import build_facts
from app.agent.report_composition import grounded_analysis
from app.tools.financial_analysis import run_analysis


ASK = '综合看看我最近半年的财务情况，最值得注意的三个问题是什么？'


def make_results(tools, records, **kwargs):
    return [{'tool': name, 'mode': '个人版', 'queryIds': ['mine'],
             'evidence': run_analysis(name, records, as_of=date(2026, 11, 19), **kwargs)}
            for name in tools]


class AttentionPointTests(unittest.TestCase):
    def test_only_explicit_bounded_attention_count_enables_selection(self):
        for phrase, n in [(ASK, 3), ('最值得关注的 6 个事项', 6), ('请列两点主要问题', 2),
                          ('帮我列出1个主要问题', 1), ('最值得注意的四个点', 4)]:
            self.assertEqual(requested_attention_count(phrase), n)
        for phrase in ['分析最近三个月的财务情况。', '列出三个主要收入来源', '最值得关注的十三个问题',
                       '不用列三个主要问题，只说明本月结余。']:
            self.assertIsNone(requested_attention_count(phrase))

    def test_real_diagnosis_selects_distinct_driver_concentration_and_small_sample(self):
        records = [{'year': 2026, 'month': m, 'day': day, 'costType': cat, 'orderRemark': name,
                    'money': amount, 'bankName': '现金'} for m in range(5, 11)
                   for day, cat, name, amount in [(1,'收入','校稿',5000), (2,'收入','家教',1000),
                                                   (3,'房租','房租',-1800)]]
        records.append({**records[-1], 'day': 7, 'costType': '医疗', 'orderRemark': '门诊', 'money': -900})
        results = make_results(['cashflow', 'income_stability', 'data_quality', 'anomalies', 'changes'], records)
        facts, findings = build_facts(results), build_analysis_findings(results)
        points = build_attention_points(ASK, results, facts, findings)
        self.assertEqual(len(points), 3)
        self.assertIn('医疗', points[0]['reason'])
        self.assertIn('900', points[0]['reason'])
        self.assertIn('校稿', points[1]['reason'])
        self.assertIn('医疗', points[2]['reason'])
        self.assertIn('样本', points[2]['reason'])
        for point in points:
            selected = [f for f in facts if f['id'] in point['factIds']]
            self.assertEqual(len({f['binding']['resultGroup'] for f in selected}), 1)
            self.assertEqual(point['analysis'], grounded_analysis(point['factIds'], facts, findings))
        self.assertFalse(build_attention_points('把这些来源列成表', results, facts, findings))

    def test_positive_net_or_wrong_scope_cannot_fill_requested_slots(self):
        records = [{'year': 2026, 'month': 10, 'day': 1, 'money': 500, 'costType': '收入', 'orderRemark': '工资'}]
        results = make_results(['cashflow'], records)
        facts, findings = build_facts(results), build_analysis_findings(results)
        self.assertEqual(len(build_attention_points(ASK, results, facts, findings)), 1)
        for field, other in [('scope', '家庭版'), ('queryIds', ['another']), ('resultGroup', 'g2')]:
            changed = [{**f, 'binding': {**f['binding'], field: other}} for f in facts]
            self.assertEqual(build_attention_points(ASK, results, changed, findings), [])
        empty = {'g1': {'tool': 'cashflow', 'scope': '个人版'}}
        self.assertEqual(build_attention_points(ASK, results, facts, empty), [])

    def test_visible_reason_names_its_scope_when_multiple_books_are_analyzed(self):
        records = [{'year': 2026, 'month': 10, 'day': 1, 'money': 500, 'costType': '收入', 'orderRemark': '工资'}]
        results = make_results(['cashflow'], records)
        results.append({**results[0], 'mode': '家庭版', 'queryIds': ['family']})
        points = build_attention_points(ASK, results, build_facts(results), build_analysis_findings(results))
        self.assertEqual(len(points), 1)
        self.assertIn('个人版', points[0]['reason'])

    def test_unmet_constraints_precede_structure_and_ledger_markup_is_literal(self):
        name = '[外部](https://example.invalid)'
        records = [{'year': 2026, 'month': m, 'day': d, 'money': n, 'costType': cat, 'orderRemark': remark}
                   for m in (8, 9, 10) for d, n, cat, remark in [(1, 100, '收入', name), (2, -200, '餐饮', '午餐')]]
        results = make_results(['cashflow', 'budget', 'purchase', 'savings'], records, target_month='2026-10',
                               budget_amount=150, starting_balance=100, reserve_amount=80, purchase_amount=50,
                               months=3, target_amount=500)
        facts, findings = build_facts(results), build_analysis_findings(results)
        points = build_attention_points('请列六个主要问题', results, facts, findings)
        reasons = [r['reason'] for r in points]
        self.assertTrue(any('预算' in s for s in reasons[:-1]))
        self.assertTrue(any('储备' in s for s in reasons[:-1]))
        self.assertTrue(any('目标' in s for s in reasons[:-1]))
        self.assertIn('` ' + name + ' `', reasons[-1])
