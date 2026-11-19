import unittest

from app.agent.report_values import literal, literal_links, quantity, sanitize_fact, value_catalog


class ReportValueTests(unittest.TestCase):
    def test_catalog_preserves_types_units_and_fact_ids_without_reading_ledger_text(self):
        facts = [
            {'id': 'earnings', 'text': '备注写着 999 元', 'values': [
                quantity('已记收入', 1250.5), quantity('参考月份', '2026-10', 'month'),
                quantity('核算日', '2026-11-19', 'date'), quantity('缺失', None),
                quantity('字符串', '999'), quantity('无效日期', '十一月', 'date')]},
            {'id': 'audit', 'values': [quantity('重复组', 0, '组'), quantity('标记', True),
                                       quantity('无穷', float('inf')), quantity('空值', float('nan'))]},
        ]
        rows = value_catalog(facts)
        self.assertEqual([(r['id'], r['factId'], r['display']) for r in rows], [
            ('v1', 'earnings', '1,250.5 元'), ('v2', 'earnings', '2026-10'),
            ('v3', 'earnings', '2026-11-19'), ('v4', 'audit', '0 组')])
        self.assertEqual((rows[0]['label'], rows[0]['value'], rows[0]['unit']), ('已记收入', 1250.5, '元'))

    def test_embedded_links_and_ledger_markdown_stay_literal(self):
        name = '[工作室](https://example.invalid)'
        note = '`金额`\n## 标题'
        data = {'evidence': {'sources': [{'source': name, 'remarks': [note]}]}}
        fact = '观察到' + name + '，备注为' + note + '。'
        self.assertEqual(sanitize_fact(fact, data), '观察到' + literal(name) + '，备注为`` `金额` ## 标题 ``。')
        self.assertEqual(literal_links('记录中的 ' + name), '记录中的 ' + literal(name))
        self.assertEqual(literal_links('备注见 https://example.invalid/'), '备注见 ` https://example.invalid/ `')
