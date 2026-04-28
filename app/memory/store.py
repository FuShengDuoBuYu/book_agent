from datetime import UTC, datetime
from uuid import uuid4

from app.memory.mongo import get_memory_db


class MongoMemoryStore:
    async def ensure_session(
        self,
        phone_num: str,
        session_id: str | None,
        first_message: str,
    ) -> str:
        db = get_memory_db()
        now = datetime.now(UTC)

        if session_id:
            session = await db.agent_sessions.find_one(
                {"_id": session_id, "phoneNum": phone_num}
            )
            if session:
                await db.agent_sessions.update_one(
                    {"_id": session_id, "phoneNum": phone_num},
                    {"$set": {"updatedAt": now}},
                )
                return session_id

        new_session_id = str(uuid4())
        await db.agent_sessions.insert_one(
            {
                "_id": new_session_id,
                "phoneNum": phone_num,
                "title": self._build_title(first_message),
                "createdAt": now,
                "updatedAt": now,
                "status": "active",
            }
        )
        return new_session_id

    async def add_message(
        self,
        phone_num: str,
        session_id: str,
        role: str,
        content: str,
        metadata: dict | None = None,
    ) -> None:
        db = get_memory_db()
        now = datetime.now(UTC)

        await db.agent_messages.insert_one(
            {
                "_id": str(uuid4()),
                "phoneNum": phone_num,
                "sessionId": session_id,
                "role": role,
                "content": content,
                "metadata": metadata or {},
                "createdAt": now,
            }
        )
        await db.agent_sessions.update_one(
            {"_id": session_id, "phoneNum": phone_num},
            {"$set": {"updatedAt": now}},
        )

    async def get_recent_messages(
        self,
        phone_num: str,
        session_id: str,
        limit: int = 6,
    ) -> list[dict]:
        db = get_memory_db()
        cursor = (
            db.agent_messages.find(
                {"phoneNum": phone_num, "sessionId": session_id},
                {"_id": 0, "role": 1, "content": 1, "metadata": 1, "createdAt": 1},
            )
            .sort("createdAt", -1)
            .limit(limit)
        )
        messages = await cursor.to_list(length=limit)
        messages.reverse()
        return messages

    def _build_title(self, message: str) -> str:
        title = message.strip().replace("\n", " ")
        return title[:30] or "新对话"
