import unittest

import httpx
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import create_app


class BrowserSessionTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(), base_url="https://bill.example.test")
        self.addCleanup(self.client.close)

    def test_chat_routes_require_a_session_before_model_execution(self):
        with patch("app.api.routes.chat.BookAgent") as agent:
            for path in ("/api/chat", "/api/chat/stream"):
                response = self.client.post(path, json={"message": "虚构账单统计"})
                self.assertEqual(response.status_code, 401)
            agent.assert_not_called()

    def test_ticket_sets_secure_cookie_and_context_returns_only_availability(self):
        with (
            patch(
                "app.api.routes.session.BookkeepingApiClient.exchange_ticket",
                AsyncMock(return_value={"token": "fixture-session", "expiresIn": 1800}),
            ) as exchange,
            patch(
                "app.api.routes.session.BookkeepingApiClient.get_session",
                AsyncMock(return_value={"phoneNum": "13800138001", "familyId": "fixture-family"}),
            ),
        ):
            launch = self.client.post(
                "/api/session/exchange",
                data={"ticket": "fixture-ticket"},
                follow_redirects=False,
            )
            self.assertEqual(launch.status_code, 303)
            self.assertEqual(launch.headers["location"], "/")
            self.assertEqual(launch.headers["cache-control"], "no-store")
            cookie = launch.headers["set-cookie"]
            for attribute in ("HttpOnly", "Secure", "Path=/", "SameSite=strict"):
                self.assertIn(attribute, cookie)
            exchange.assert_awaited_once_with("fixture-ticket")

            context = self.client.get("/api/session")
            self.assertEqual(context.json(), {"familyAvailable": True})
            self.assertEqual(context.headers["cache-control"], "no-store")

    def test_browser_cannot_specify_ledger_identity(self):
        with (
            patch(
                "app.api.routes.session.BookkeepingApiClient.get_session",
                AsyncMock(return_value={"phoneNum": "13800138001", "familyId": ""}),
            ),
            patch("app.api.routes.chat.BookAgent") as agent,
        ):
            self.client.cookies.set("__Host-book-agent", "fixture-session")
            response = self.client.post("/api/chat", json={
                "message": "虚构账单统计",
                "phoneNum": "13800138002",
                "familyId": "another-family",
            })
            self.assertEqual(response.status_code, 422)
            agent.assert_not_called()

    def test_valid_session_uses_ledger_account(self):
        with (
            patch(
                "app.api.routes.session.BookkeepingApiClient.get_session",
                AsyncMock(return_value={"phoneNum": "13800138001", "familyId": ""}),
            ),
            patch("app.api.routes.chat.BookAgent") as agent,
        ):
            self.client.cookies.set("__Host-book-agent", "fixture-session")
            agent.return_value.chat = AsyncMock(return_value=("fixture", "chat-id"))
            response = self.client.post("/api/chat", json={"message": "虚构账单统计"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"reply": "fixture", "sessionId": "chat-id"})
            agent.return_value.chat.assert_awaited_once_with(
                phone_num="13800138001",
                message="虚构账单统计",
                session_id=None,
                mode="个人版",
                family_id="",
            )


    def test_expired_session_is_rejected_without_starting_chat(self):
        request = httpx.Request("GET", "https://ledger.example.test/ai-bill/session")
        response = httpx.Response(401, request=request)
        with (
            patch(
                "app.api.routes.session.BookkeepingApiClient.get_session",
                AsyncMock(side_effect=httpx.HTTPStatusError("expired", request=request, response=response)),
            ),
            patch("app.api.routes.chat.BookAgent") as agent,
        ):
            self.client.cookies.set("__Host-book-agent", "expired-fixture")
            result = self.client.post("/api/chat/stream", json={"message": "虚构账单统计"})
            self.assertEqual(result.status_code, 401)
            self.assertEqual(result.headers["cache-control"], "no-store")
            agent.assert_not_called()

    def test_authenticated_stream_preserves_sse(self):
        async def fixture_events(**kwargs):
            yield {"type": "delta", "content": "12.50"}
            yield {"type": "done", "content": ""}

        with (
            patch(
                "app.api.routes.session.BookkeepingApiClient.get_session",
                AsyncMock(return_value={"phoneNum": "13800138001", "familyId": ""}),
            ),
            patch("app.api.routes.chat.BookAgent") as agent,
        ):
            self.client.cookies.set("__Host-book-agent", "fixture-session")
            agent.return_value.stream_chat = fixture_events
            response = self.client.post("/api/chat/stream", json={"message": "虚构账单统计"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream", response.headers["content-type"])
            self.assertEqual(response.headers["x-accel-buffering"], "no")
            self.assertIn('"content": "12.50"', response.text)
            self.assertIn('"type": "done"', response.text)
