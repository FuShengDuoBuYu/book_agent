import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.memory.store import MongoMemoryStore, _page_filter


class ConversationApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(), base_url="https://bill.example.test")
        self.addCleanup(self.client.close)
        self.account = {"phoneNum": "13800138001", "familyId": "fixture-family"}
        self.profile = patch("app.api.routes.session.BookkeepingApiClient.get_session", AsyncMock(return_value=self.account))
        self.profile.start()
        self.addCleanup(self.profile.stop)

    def test_history_requires_authentication(self):
        for path in ("/api/conversations", "/api/conversations/unknown"):
            self.assertEqual(self.client.get(path).status_code, 401)

    def test_history_uses_verified_account_and_scope(self):
        self.client.cookies.set("__Host-book-agent", "fixture")
        with patch.object(MongoMemoryStore, "list_sessions", AsyncMock(return_value={"items": [], "nextCursor": None})) as read:
            response = self.client.get("/api/conversations", params={"mode": "家庭版"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(read.await_args.kwargs["phone_num"], self.account["phoneNum"])
        self.assertEqual(read.await_args.kwargs["book_scope"], "family:fixture-family")

    def test_foreign_or_obsolete_scope_is_not_readable(self):
        self.client.cookies.set("__Host-book-agent", "fixture")
        with patch.object(MongoMemoryStore, "get_session_messages", AsyncMock(return_value=None)):
            self.assertEqual(self.client.get("/api/conversations/foreign").status_code, 404)

    def test_family_history_requires_current_membership(self):
        self.account["familyId"] = ""
        self.client.cookies.set("__Host-book-agent", "fixture")
        self.assertEqual(self.client.get("/api/conversations", params={"mode": "家庭版"}).status_code, 403)


class ConversationStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_pagination_keeps_same_timestamp_messages_and_hides_metadata(self):
        timestamp = datetime(2026, 11, 12, tzinfo=UTC)
        rows = [{"_id": key, "role": "assistant", "content": key, "createdAt": timestamp,
                 "phoneNum": "private", "metadata": {"private": True}} for key in ("c", "b", "a")]
        cursor = Mock()
        cursor.sort.return_value = cursor
        cursor.limit.return_value = cursor
        cursor.to_list = AsyncMock(return_value=rows)
        messages = SimpleNamespace(find=Mock(return_value=cursor))
        sessions = SimpleNamespace(find_one=AsyncMock(return_value={"title": "已有对话"}))
        with patch("app.memory.store.get_memory_db", return_value=SimpleNamespace(agent_sessions=sessions, agent_messages=messages)):
            result = await MongoMemoryStore().get_session_messages("fixture", "personal", "chat", limit=2)
        self.assertEqual([row["id"] for row in result["messages"]], ["b", "c"])
        self.assertEqual(_page_filter(result["nextCursor"], "createdAt")["$or"][1]["_id"], {"$lt": "b"})
        self.assertFalse({"phoneNum", "metadata"} & result["messages"][0].keys())

    async def test_new_conversation_does_not_resume_prior_chat(self):
        sessions = SimpleNamespace(find_one=AsyncMock(), insert_one=AsyncMock())
        with patch("app.memory.store.get_memory_db", return_value=SimpleNamespace(agent_sessions=sessions)):
            await MongoMemoryStore().ensure_session("fixture", None, "新问题", "personal", new_session=True)
        sessions.find_one.assert_not_called()
        sessions.insert_one.assert_awaited_once()

    async def test_foreign_session_stops_before_reading_messages(self):
        messages = SimpleNamespace(find=AsyncMock())
        sessions = SimpleNamespace(find_one=AsyncMock(return_value=None))
        with patch("app.memory.store.get_memory_db", return_value=SimpleNamespace(agent_sessions=sessions, agent_messages=messages)):
            result = await MongoMemoryStore().get_session_messages("fixture", "personal", "foreign")
        self.assertIsNone(result)
        messages.find.assert_not_called()
        self.assertEqual(sessions.find_one.await_args.args[0], {"_id": "foreign", "phoneNum": "fixture", "bookScope": "personal"})
