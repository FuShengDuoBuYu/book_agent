import json
import unittest
from unittest.mock import patch

import httpx

from app.agent.book_agent import BookAgent
from app.clients.bookkeeping_api import BookkeepingApiClient
from app.schemas.plan import AgentPlan, SearchPersonalOrdersArgs, ToolCall


class LedgerAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_cannot_change_the_selected_ledger(self):
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
        plan = AgentPlan(toolCalls=[ToolCall(
            toolName="search_family_orders",
            args=SearchPersonalOrdersArgs(year=2026, month=10),
        )])
        factory = httpx.AsyncClient
        with patch(
            "app.clients.bookkeeping_api.httpx.AsyncClient",
            side_effect=lambda **kwargs: factory(transport=httpx.MockTransport(ledger), **kwargs),
        ):
            result = json.loads(await agent._execute_tool_calls("个人版", "", plan))

        request, body = observed[0]
        self.assertEqual(request.url.path, "/ai-bill/orders")
        self.assertEqual(request.headers["authorization"], "Bearer fixture-grant")
        self.assertEqual(body["mode"], "个人版")
        self.assertFalse({"phoneNum", "familyId", "userId"} & body.keys())
        self.assertEqual(result["toolResults"][0]["orderCount"], 1)
        self.assertNotIn("13800138001", json.dumps(result))

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
