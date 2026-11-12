import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.memory.store import MongoMemoryStore


class MemoryScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_uses_account_and_book_scope(self):
        sessions = SimpleNamespace(
            find_one=AsyncMock(return_value=None),
            insert_one=AsyncMock(),
        )
        with patch("app.memory.store.get_memory_db", return_value=SimpleNamespace(agent_sessions=sessions)):
            result = await MongoMemoryStore().ensure_session(
                phone_num="13800138001",
                session_id="prior-chat",
                first_message="测试",
                book_scope="personal",
            )

        sessions.find_one.assert_awaited_once_with({
            "_id": "prior-chat", "phoneNum": "13800138001", "bookScope": "personal",
        })
        self.assertNotEqual(result, "prior-chat")
        self.assertEqual(sessions.insert_one.await_args.args[0]["bookScope"], "personal")
