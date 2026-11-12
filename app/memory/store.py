from datetime import UTC, datetime
import base64
import binascii
import json
from uuid import uuid4

from app.memory.mongo import get_memory_db


def conversation_scope(mode: str, family_id: str | None) -> str:
    scope = "family" if mode == "家庭版" else "personal"
    return f"{scope}:{family_id}" if family_id else scope


def _page_filter(cursor: str | None, field: str) -> dict:
    if not cursor:
        return {}
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor))
        timestamp = datetime.fromisoformat(value["time"])
        identifier = value["id"]
        if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
            raise ValueError("Invalid identifier")
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise ValueError("Invalid history cursor") from exc
    return {"$or": [{field: {"$lt": timestamp}}, {field: timestamp, "_id": {"$lt": identifier}}]}


def _next_cursor(rows: list[dict], limit: int, field: str) -> str | None:
    if len(rows) <= limit:
        return None
    last = rows[limit - 1]
    payload = json.dumps({"time": last[field].isoformat(), "id": last["_id"]})
    return base64.urlsafe_b64encode(payload.encode()).decode()


class MongoMemoryStore:
    async def ensure_session(
        self,
        phone_num: str,
        session_id: str | None,
        first_message: str,
        book_scope: str,
        new_session: bool = False,
    ) -> str:
        # session 的职责是把一轮轮消息归到同一个会话里，便于多轮上下文和前端续聊。
        db = get_memory_db()
        now = datetime.now(UTC)

        owner = {"phoneNum": phone_num, "bookScope": book_scope}
        session = None
        if session_id and not new_session:
            session = await db.agent_sessions.find_one({"_id": session_id, **owner})
        elif not new_session:
            session = await db.agent_sessions.find_one(
                {**owner, "status": "active"}, sort=[("updatedAt", -1)]
            )
        if session:
            resolved_session_id = session["_id"]
            await db.agent_sessions.update_one(
                {"_id": resolved_session_id, **owner},
                {"$set": {"updatedAt": now}},
            )
            return resolved_session_id

        new_session_id = str(uuid4())
        await db.agent_sessions.insert_one(
            {
                "_id": new_session_id,
                "phoneNum": phone_num,
                "bookScope": book_scope,
                "title": self._build_title(first_message),
                "createdAt": now,
                "updatedAt": now,
                "status": "active",
            }
        )
        return new_session_id

    async def list_sessions(self, phone_num: str, book_scope: str, limit: int = 30, cursor: str | None = None) -> dict:
        query = {"phoneNum": phone_num, "bookScope": book_scope, **_page_filter(cursor, "updatedAt")}
        rows = await (get_memory_db().agent_sessions.find(query, {"_id": 1, "title": 1, "createdAt": 1, "updatedAt": 1})
                      .sort([("updatedAt", -1), ("_id", -1)]).limit(limit + 1).to_list(limit + 1))
        return {"items": [{"id": row["_id"], "title": row.get("title") or "历史对话",
                           "updatedAt": row["updatedAt"].replace(tzinfo=UTC) if row.get("updatedAt") else None} for row in rows[:limit]],
                "nextCursor": _next_cursor(rows, limit, "updatedAt")}

    async def get_session_messages(self, phone_num: str, book_scope: str, session_id: str,
                                   limit: int = 50, cursor: str | None = None) -> dict | None:
        db = get_memory_db()
        session = await db.agent_sessions.find_one({"_id": session_id, "phoneNum": phone_num, "bookScope": book_scope})
        if not session:
            return None
        query = {"phoneNum": phone_num, "sessionId": session_id, **_page_filter(cursor, "createdAt")}
        rows = await (db.agent_messages.find(query, {"_id": 1, "role": 1, "content": 1, "createdAt": 1})
                      .sort([("createdAt", -1), ("_id", -1)]).limit(limit + 1).to_list(limit + 1))
        return {"sessionId": session_id, "title": session.get("title") or "历史对话",
                "messages": [{"id": row["_id"], "role": row["role"], "content": row["content"],
                              "createdAt": row.get("createdAt")} for row in reversed(rows[:limit])],
                "nextCursor": _next_cursor(rows, limit, "createdAt")}

    async def add_message(
        self,
        phone_num: str,
        session_id: str,
        role: str,
        content: str,
        metadata: dict | None = None,
    ) -> None:
        # 消息和 session 分表存储：前者保存聊天内容，后者保存会话元信息。
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
        # 读取时先倒序取最近 N 条，再 reverse 回正序，方便直接喂给模型。
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
        # 会话标题直接从首条用户消息截取，简单但足够用于列表展示。
        title = message.strip().replace("\n", " ")
        return title[:30] or "新对话"
