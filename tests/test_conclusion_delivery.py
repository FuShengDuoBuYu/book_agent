import unittest
from unittest.mock import AsyncMock, patch

from app.agent.artifacts import build_artifacts
from app.agent.evidence_report import EvidenceReporter
from tests.test_analysis_artifacts import forecast_result


def statement(text, *ids):
    return {"text": text, "factIds": list(ids)}


def answer(text, ids=("f1",), reasons=None, caveat=None):
    return {"conclusion": statement(text, *ids), "reasons": reasons or [],
            "nextStep": None, "caveat": caveat}


class ConclusionDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_forecast_leads_with_synthesis_not_audit_inventory(self):
        reporter = EvidenceReporter()

        async def invoke(schema, system, payload):
            value = next((key for key, value in payload.get("values", {}).items()
                          if "预测基准 =" in value), "v1")
            return answer("{{j1}}", reasons=[statement("先按{{" + value + "}}安排下个月的必要开销，临时收入到账后再增加预算。", "f1")],
                          caveat=statement("这是历史模式延续的条件估计，不能保证未来到账。", "f1"))

        reporter._invoke = AsyncMock(side_effect=invoke)
        forecast = forecast_result()
        reply, metadata = await reporter.answer("预计下个月收入多少？", {}, [forecast],
                                               artifacts=build_artifacts([forecast]))
        self.assertIn("先按", reply)
        self.assertIn("7,100", reply)
        self.assertLessEqual(len(reply), 700)
        self.assertNotIn("| 成员 |", reply)
        self.assertNotIn("{{", reply)
        self.assertTrue(metadata["facts"])

    async def test_data_stays_separate_even_when_user_explicitly_requests_a_table(self):
        reporter = EvidenceReporter()
        reporter._invoke = AsyncMock(side_effect=TimeoutError())
        forecast = forecast_result()
        artifacts = build_artifacts([forecast], {"table": True})
        reply, metadata = await reporter.answer("把预测来源列成表", {}, [forecast], artifacts=artifacts)
        self.assertNotIn("| ---", reply)
        self.assertTrue(artifacts[0]["rows"])
        self.assertEqual(metadata["compositionStatus"], "fallback")

    async def test_timeout_with_large_audit_keeps_short_answer_and_complete_private_evidence(self):
        reporter = EvidenceReporter()
        reporter._invoke = AsyncMock(side_effect=TimeoutError())
        facts = [{"id": f"f{i}", "text": "【家庭版】供审计的历史来源描述。" * 8,
                  "required": True, "values": [],
                  "binding": {"resultGroup": "g1", "scope": "家庭版", "tool": "forecast", "primary": True}}
                 for i in range(1, 80)]
        with patch("app.agent.evidence_report.build_facts", return_value=facts):
            reply, metadata = await reporter.answer("直接给结论", {}, [forecast_result()])
        self.assertLessEqual(len(reply), 1200)
        self.assertEqual(len(metadata["facts"]), 79)
        self.assertNotIn("以下是本次账本核算与分析", reply)

    async def test_unknown_balance_never_becomes_a_purchase_verdict_without_numbers(self):
        reporter = EvidenceReporter()
        reporter._invoke = AsyncMock(return_value=answer("放心买，完全不会影响生活。"))
        result = {"tool": "purchase", "mode": "家庭版", "evidence": {
            "sufficientData": False, "missingInputs": ["startingBalance"], "purchaseAmount": 700}}
        reply, metadata = await reporter.answer("这东西我能不能买？", {}, [result])
        self.assertNotIn("放心买", reply)
        self.assertNotIn("完全不会影响生活", reply)
        self.assertIn("余额", reply)
        self.assertEqual(metadata["compositionStatus"], "fallback")

    async def test_primary_source_analysis_is_not_discarded_by_an_empty_advisory_need_list(self):
        reporter = EvidenceReporter()
        reporter._invoke = AsyncMock(side_effect=TimeoutError())
        result = {'tool': 'sources', 'mode': '个人版', 'evidence': {
            'sources': [{'metric': 'income', 'source': '项目收入', 'amount': 900, 'remarks': ['项目结算']}],
            'memberTotals': []}}
        _, meta = await reporter.answer('把收入来源列成表', {}, [result],
            understanding={'requestedAnswers': ['展示来源表格'], 'evidenceNeeds': []})
        self.assertTrue(meta['facts'])

    async def test_two_scopes_cannot_share_a_typed_value_by_citing_another_fact(self):
        reporter = EvidenceReporter()

        async def invoke(schema, system, payload):
            value = next((key for key, value in payload.get("values", {}).items()
                          if value.startswith("foreign ")), "v9999")
            return answer("个人下月收入是{{" + value + "}}。")

        reporter._invoke = AsyncMock(side_effect=invoke)
        base = [{"id": "f1", "text": "家庭预测基准。", "required": True,
                 "values": [{"label": "预测基准", "value": 7100, "unit": "元"}],
                 "binding": {"resultGroup": "g1", "scope": "家庭版", "tool": "forecast", "primary": True}},
                {"id": "foreign", "text": "另一账本历史入账。", "required": False,
                 "values": [{"label": "历史入账", "value": 999999, "unit": "元"}],
                 "binding": {"resultGroup": "g1", "scope": "个人版", "tool": "forecast", "primary": False}}]
        with patch("app.agent.evidence_report.build_facts", return_value=base):
            reply, metadata = await reporter.answer("家庭预测有多少？", {}, [forecast_result()])
        self.assertNotIn("个人下月收入是", reply)
        self.assertEqual(metadata["compositionStatus"], "fallback")


class BudgetEvidenceTests(unittest.TestCase):
    def test_empty_target_month_does_not_publish_assumed_full_remaining_budget(self):
        from datetime import date
        from app.tools.financial_analysis import run_analysis
        from app.agent.evidence_report import build_facts
        result = {'tool': 'budget', 'mode': '个人版', 'evidence': run_analysis(
            'budget', [], as_of=date(2026, 11, 20), budget_amount=4000)}
        facts = build_facts([result])
        text = '\n'.join(f['text'] for f in facts)
        self.assertIn('未知', text)
        self.assertNotIn('已记支出 0', text)
        self.assertFalse(any(v['label'] in {'spent', 'remaining', 'dailyAllowance'}
                             for f in facts for v in f.get('values', [])))


class SupportingArtifactTests(unittest.TestCase):
    def test_source_table_request_does_not_expand_original_remark_mapping(self):
        source = {'tool': 'sources', 'mode': '个人版', 'evidence': {
            'sources': [{'metric': 'income', 'source': '项目收入', 'amount': 900, 'remarks': ['项目结算']}],
            'memberTotals': []}}
        artifacts = build_artifacts([source], {'table': True, 'includeLineage': True})
        mapping = next(a for a in artifacts if '原始备注' in a['title'])
        self.assertEqual(mapping.get('presentation'), 'supporting')
        explicit = build_artifacts([source], {'table': True, 'includeLineage': True, 'lineageRequested': True})
        self.assertTrue(any(a.get('presentation') == 'requested' for a in explicit if '原始备注' in a['title']))

    def test_unrequested_forecast_data_is_supporting_and_requested_table_expands(self):
        ordinary = build_artifacts([forecast_result()], {"table": False, "chart": "none"})
        requested = build_artifacts([forecast_result()], {"table": True})
        self.assertTrue(ordinary)
        self.assertTrue(all(item.get("presentation") == "supporting" for item in ordinary))
        self.assertTrue(any(item.get("presentation") == "requested" for item in requested))


class LegacyChatTableTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_chat_keeps_explicit_tables_but_does_not_dump_supporting_data(self):
        from app.agent.book_agent import BookAgent
        agent = BookAgent.__new__(BookAgent)
        for presentation in ['supporting', 'requested']:
            async def events(**kwargs):
                yield {'type': 'session', 'sessionId': 'fixture'}
                yield {'type': 'delta', 'content': '简短结论。'}
                yield {'type': 'artifacts', 'artifacts': [{'id': 'a1', 'kind': 'table', 'title': '请求的数据',
                    'presentation': presentation, 'columns': [{'key': 'amount', 'label': '金额', 'unit': '元'}],
                    'rows': [{'amount': 900}]}]}
            agent.stream_chat = events
            reply, _ = await agent.chat('fixture', '问题')
            self.assertEqual('| ---' in reply, presentation == 'requested')


if __name__ == "__main__":
    unittest.main()
