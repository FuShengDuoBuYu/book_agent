import json
import unittest
from unittest.mock import patch

import httpx

from app.agent.book_agent import BookAgent
from app.clients.bookkeeping_api import BookkeepingApiClient
from app.schemas.plan import AgentPlan, SearchPersonalOrdersArgs, ToolCall


class LedgerAccessTests(unittest.IsolatedAsyncioTestCase):
    def test_insufficient_analysis_requests_inputs_without_model_guesses(self):
        agent = BookAgent.__new__(BookAgent)
        payload = json.dumps({"ok": True, "toolResults": [{"ok": True, "userFound": True, "orderCount": 1}],
                              "analysisResults": [{"tool": "budget", "evidence": {"sufficientData": False, "missingInputs": ["budgetAmount"]}}]})
        reply = agent._build_deterministic_reply(AgentPlan(), payload)
        self.assertIn("预算金额", reply)
        self.assertNotIn("0元", reply)

    async def test_analysis_uses_its_bound_query_filter(self):
        from unittest.mock import AsyncMock
        agent = BookAgent(BookkeepingApiClient("fixture"))
        food = {"year": 2026, "month": 11, "day": 1, "money": -200, "costType": "餐饮"}
        rent = {**food, "money": -1000, "costType": "房租"}
        async def lookup(call, family_id):
            records = [food] if call.id == "food" else [food, rent]
            return {"ok": True, "userFound": True, "mode": "个人版", "ordersInfo": records, "orderCount": len(records)}
        plan = AgentPlan.model_validate({"toolCalls": [
            {"id": "food", "toolName": "search_personal_orders", "args": {"cost_type": "餐饮"}},
            {"id": "all", "toolName": "search_personal_orders"},
        ], "analyses": [{"tool": "budget", "budgetAmount": 500, "targetMonth": "2026-11", "queryIds": ["food"]},
                        {"tool": "forecast", "queryIds": ["all"]}]})
        with patch.object(agent, "_execute_tool_call", lookup):
            payload = json.loads(await agent._execute_tool_calls("个人版", "", plan))
        self.assertEqual(payload["analysisResults"][0]["evidence"]["remaining"], 300)

    async def test_normal_chat_entry_runs_tools_and_persists_evidence(self):
        from unittest.mock import AsyncMock
        from scripts.evaluate_analysis import FixtureLedger, FixtureMemory

        agent = BookAgent(FixtureLedger())
        agent.memory = FixtureMemory()
        agent.reporter.answer = AsyncMock(return_value=('合成账本分析完成', {'status': 'reviewed'}))
        plan = AgentPlan.model_validate({"analyses": [{"tool": "cashflow"}],
            "toolCalls": [{"toolName": "search_personal_orders"}]})
        with patch.object(agent, "_create_plan", AsyncMock(return_value=plan)):
            reply, _ = await agent.chat("fixture", "分析收支")
        self.assertEqual(reply, "合成账本分析完成")
        metadata = agent.memory.messages[-1]["metadata"]
        self.assertTrue(metadata["evidence"]["analysisResults"])
        self.assertIsInstance(metadata["durationMs"], int)

    async def test_analysis_payload_is_safe_for_answer_generation(self):
        agent = BookAgent(BookkeepingApiClient("fixture"))
        plan = AgentPlan.model_validate({"analyses": [{"tool": "cashflow"}],
            "toolCalls": [{"toolName": "search_personal_orders"}]})
        from unittest.mock import AsyncMock
        with patch.object(agent, "_execute_tool_call", AsyncMock(return_value={
            "ok": True, "userFound": True, "mode": "个人版", "orderCount": 1,
            "ordersInfo": [{"year": 2026, "month": 10, "day": 1, "money": 100, "costType": "其他"}],
        })):
            payload = await agent._execute_tool_calls("个人版", "", plan)
        agent._assert_no_raw_orders_for_answer(payload)
        self.assertEqual(json.loads(payload)["analysisResults"][0]["evidence"]["incomeTotal"], 100)

    async def test_mixed_plan_reads_distinct_authorized_scopes(self):
        observed = []

        async def ledger(request):
            observed.append((request, json.loads(request.content)))
            return httpx.Response(200, json={
                "success": True,
                "message": "查询成功",
                "data": {
                    "userInfo": [{"phoneNum": "13800138001"}],
                    "ordersInfo": [{
                        "id": 1, "year": 2026, "month": 10, "day": 1,
                        "money": -12.5, "userId": "13800138001",
                    }],
                },
            })

        agent = BookAgent(BookkeepingApiClient("fixture-grant"))
        plan = AgentPlan(toolCalls=[
            ToolCall(toolName=tool, args=SearchPersonalOrdersArgs(year=2026, month=10))
            for tool in ("search_personal_orders", "search_family_orders")
        ])
        factory = httpx.AsyncClient
        with patch(
            "app.clients.bookkeeping_api.httpx.AsyncClient",
            side_effect=lambda **kwargs: factory(transport=httpx.MockTransport(ledger), **kwargs),
        ):
            result = json.loads(await agent._execute_tool_calls("个人版", "fixture-family", plan))

        personal, family = observed
        self.assertEqual([body["mode"] for _, body in observed], ["个人版", "家庭版"])
        self.assertNotIn("x-ai-bill-family-scope", personal[0].headers)
        self.assertEqual(family[0].headers["x-ai-bill-family-scope"], "fixture-family")
        for request, body in observed:
            self.assertEqual(request.url.path, "/ai-bill/orders")
            self.assertEqual(request.headers["authorization"], "Bearer fixture-grant")
            self.assertFalse({"phoneNum", "familyId", "userId"} & body.keys())
        self.assertEqual(result["toolResults"][0]["orderCount"], 1)
        self.assertNotIn("13800138001", json.dumps(result))

    async def test_family_lookup_requires_verified_membership(self):
        agent = BookAgent(BookkeepingApiClient("fixture-grant"))
        plan = AgentPlan(toolCalls=[ToolCall(toolName="search_family_orders")])
        with patch.object(agent.ledger_client, "search_orders") as lookup:
            result = await agent._execute_tool_calls("个人版", "", plan)
        lookup.assert_not_called()
        self.assertIn("未加入家庭", agent._build_deterministic_reply(plan, result))

    async def test_unavailable_ledger_is_not_reported_as_empty(self):
        async def ledger(request):
            return httpx.Response(503)

        agent = BookAgent(BookkeepingApiClient("fixture-grant"))
        plan = AgentPlan(toolCalls=[ToolCall(toolName="search_personal_orders")])
        factory = httpx.AsyncClient
        with patch(
            "app.clients.bookkeeping_api.httpx.AsyncClient",
            side_effect=lambda **kwargs: factory(transport=httpx.MockTransport(ledger), **kwargs),
        ):
            result = await agent._execute_tool_calls("个人版", "", plan)

        self.assertIn("暂不可用", agent._build_deterministic_reply(plan, result))
