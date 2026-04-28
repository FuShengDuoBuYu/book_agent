import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from langchain_core.prompts import ChatPromptTemplate

from app.agent.prompts import BOOK_AGENT_ANALYSIS_PROMPT, BOOK_AGENT_SYSTEM_PROMPT
from app.core.llm import get_chat_model
from app.tools.personal_orders import search_personal_orders


class BookAgent:
    def __init__(self) -> None:
        self.prompt = ChatPromptTemplate.from_messages(
            [
                ("system", BOOK_AGENT_SYSTEM_PROMPT),
                (
                    "human",
                    BOOK_AGENT_ANALYSIS_PROMPT,
                ),
            ]
        )
        self.model = get_chat_model()
        self.chain = self.prompt | self.model

    async def chat(self, phone_num: str, message: str) -> str:
        chunks: list[str] = []
        async for event in self.stream_chat(phone_num=phone_num, message=message):
            if event["type"] == "delta":
                chunks.append(event["content"])
            elif event["type"] == "error":
                raise RuntimeError(event["content"])

        return "".join(chunks).strip()

    async def stream_chat(
        self, phone_num: str, message: str
    ) -> AsyncIterator[dict[str, str]]:
        try:
            yield {"type": "status", "content": "正在解析你的问题"}
            query = self._build_query(message)

            yield {
                "type": "status",
                "content": f"正在查询真实账单 API：{query['summary']}",
            }
            orders_json = await search_personal_orders(
                phone_num=phone_num,
                year=query["year"],
                month=query["month"],
                day=query["day"],
            )
            order_count = self._extract_order_count(orders_json)
            yield {"type": "status", "content": f"已获取 {order_count} 条账单"}
            yield {"type": "status", "content": "正在生成分析结果"}

            async for chunk in self.chain.astream(
                {
                    "message": message,
                    "query_summary": query["summary"],
                    "orders_json": orders_json,
                }
            ):
                content = getattr(chunk, "content", "")
                if content:
                    yield {"type": "delta", "content": content}

            yield {"type": "done", "content": ""}
        except Exception as exc:
            error_text = str(exc)
            if "Connection refused" in error_text or "Failed to connect" in error_text:
                yield {
                    "type": "error",
                    "content": "无法连接 Ollama。请确认已运行：ollama run qwen3:14b",
                }
                return

            yield {"type": "error", "content": f"Agent 调用失败：{error_text}"}

    def _build_query(self, message: str) -> dict[str, int | None | str]:
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        text = message.strip()

        if any(keyword in text for keyword in ["全部", "所有", "历史"]):
            return {"year": None, "month": None, "day": None, "summary": "全部个人账单"}

        if "昨天" in text or "昨日" in text:
            target = now - timedelta(days=1)
            return {
                "year": target.year,
                "month": target.month,
                "day": target.day,
                "summary": f"{target.year}年{target.month}月{target.day}日个人账单",
            }

        if "今天" in text or "今日" in text:
            return {
                "year": now.year,
                "month": now.month,
                "day": now.day,
                "summary": f"{now.year}年{now.month}月{now.day}日个人账单",
            }

        if "今年" in text:
            return {"year": now.year, "month": None, "day": None, "summary": f"{now.year}年个人账单"}

        return {"year": now.year, "month": now.month, "day": None, "summary": f"{now.year}年{now.month}月个人账单"}

    def _extract_order_count(self, orders_json: str) -> int:
        try:
            data = json.loads(orders_json)
        except json.JSONDecodeError:
            return 0

        return int(data.get("orderCount") or 0)
