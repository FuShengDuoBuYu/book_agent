import json
from collections.abc import AsyncIterator

from langchain_core.prompts import ChatPromptTemplate

from app.agent.planner import QueryPlanner
from app.agent.prompts import BOOK_AGENT_ANALYSIS_PROMPT, BOOK_AGENT_SYSTEM_PROMPT
from app.core.llm import get_chat_model
from app.memory.store import MongoMemoryStore
from app.schemas.plan import AgentPlan, ToolCall
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
        self.memory = MongoMemoryStore()
        self.planner = QueryPlanner()

    async def chat(
        self,
        phone_num: str,
        message: str,
        session_id: str | None = None,
    ) -> tuple[str, str | None]:
        chunks: list[str] = []
        resolved_session_id: str | None = None
        async for event in self.stream_chat(
            phone_num=phone_num,
            message=message,
            session_id=session_id,
        ):
            if event["type"] == "session":
                resolved_session_id = event["sessionId"]
            if event["type"] == "delta":
                chunks.append(event["content"])
            elif event["type"] == "error":
                raise RuntimeError(event["content"])

        return "".join(chunks).strip(), resolved_session_id

    async def stream_chat(
        self,
        phone_num: str,
        message: str,
        session_id: str | None = None,
    ) -> AsyncIterator[dict[str, str]]:
        resolved_session_id: str | None = None
        assistant_chunks: list[str] = []
        try:
            yield {"type": "status", "content": "正在解析你的问题"}
            resolved_session_id = await self.memory.ensure_session(
                phone_num=phone_num,
                session_id=session_id,
                first_message=message,
            )
            yield {"type": "session", "sessionId": resolved_session_id}
            history_messages = await self.memory.get_recent_messages(
                phone_num=phone_num,
                session_id=resolved_session_id,
                limit=6,
            )
            yield {
                "type": "status",
                "content": f"已加载 {len(history_messages)} 条会话历史",
            }
            history_text = self._format_history(history_messages)
            await self.memory.add_message(
                phone_num=phone_num,
                session_id=resolved_session_id,
                role="user",
                content=message,
            )

            yield {"type": "status", "content": "正在规划账单查询"}
            plan = await self.planner.create_plan(message=message, history=history_text)
            yield {
                "type": "status",
                "content": f"查询计划：{plan.summary}",
            }

            if plan.needsTools:
                orders_json = await self._execute_tool_calls(phone_num, plan)
                order_count = self._extract_order_count(orders_json)
                yield {
                    "type": "status",
                    "content": (
                        f"已执行 {len(plan.toolCalls)} 个工具调用，"
                        f"共获取 {order_count} 条账单"
                    ),
                }
            else:
                orders_json = json.dumps(
                    {
                        "ok": True,
                        "message": "本轮无需查询账单。",
                        "ordersInfo": [],
                        "orderCount": 0,
                    },
                    ensure_ascii=False,
                )
            yield {"type": "status", "content": "正在生成分析结果"}

            think_parser = ThinkTagStreamParser()
            async for chunk in self.chain.astream(
                {
                    "message": message,
                    "history": history_text,
                    "query_summary": plan.summary,
                    "plan_json": plan.model_dump_json(),
                    "orders_json": orders_json,
                }
            ):
                content = getattr(chunk, "content", "")
                if content:
                    for event in think_parser.feed(content):
                        if event["type"] == "delta":
                            assistant_chunks.append(event["content"])
                        yield event

            for event in think_parser.flush():
                if event["type"] == "delta":
                    assistant_chunks.append(event["content"])
                yield event

            assistant_reply = "".join(assistant_chunks).strip()
            if assistant_reply and resolved_session_id:
                await self.memory.add_message(
                    phone_num=phone_num,
                    session_id=resolved_session_id,
                    role="assistant",
                    content=assistant_reply,
                    metadata={"plan": plan.model_dump(mode="json")},
                )

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

    def _extract_order_count(self, orders_json: str) -> int:
        try:
            data = json.loads(orders_json)
        except json.JSONDecodeError:
            return 0

        if "totalOrderCount" in data:
            return int(data.get("totalOrderCount") or 0)
        return int(data.get("orderCount") or 0)

    async def _execute_tool_calls(self, phone_num: str, plan: AgentPlan) -> str:
        tool_results = []
        total_order_count = 0

        for tool_call in plan.toolCalls:
            raw_result = await self._execute_tool_call(phone_num, tool_call)
            parsed_result = self._safe_json_loads(raw_result)
            order_count = int(parsed_result.get("orderCount") or 0)
            total_order_count += order_count
            tool_results.append(
                {
                    "toolCallId": tool_call.id,
                    "toolName": tool_call.toolName,
                    "reason": tool_call.reason,
                    "args": tool_call.args.model_dump(mode="json"),
                    "summary": self._format_tool_call(tool_call),
                    "orderCount": order_count,
                    "result": parsed_result,
                }
            )

        return json.dumps(
            {
                "ok": True,
                "message": "账单查询完成。",
                "planSummary": plan.summary,
                "analysisType": plan.analysisType,
                "finalInstruction": plan.finalInstruction,
                "totalOrderCount": total_order_count,
                "toolResults": tool_results,
            },
            ensure_ascii=False,
            default=str,
        )

    async def _execute_tool_call(self, phone_num: str, tool_call: ToolCall) -> str:
        if tool_call.toolName != "search_personal_orders":
            return json.dumps(
                {
                    "ok": False,
                    "error": "unsupported_tool",
                    "toolName": tool_call.toolName,
                },
                ensure_ascii=False,
            )

        args = tool_call.args
        return await search_personal_orders(
            phone_num=phone_num,
            year=args.year,
            month=args.month,
            day=args.day,
            cost_type=args.cost_type,
            remark=args.remark,
        )

    def _safe_json_loads(self, value: str) -> dict:
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            return {"ok": False, "error": "invalid_tool_json", "raw": value}
        return data if isinstance(data, dict) else {"ok": False, "raw": data}

    def _format_tool_call(self, tool_call: ToolCall) -> str:
        args = tool_call.args
        if args.year is None:
            return "全部个人账单"
        if args.month is None:
            return f"{args.year}年个人账单"
        if args.day is None:
            return f"{args.year}年{args.month}月个人账单"
        return f"{args.year}年{args.month}月{args.day}日个人账单"

    def _format_history(self, messages: list[dict]) -> str:
        if not messages:
            return "无历史消息。"

        lines = []
        for item in messages:
            role = "用户" if item.get("role") == "user" else "助手"
            content = str(item.get("content", "")).strip()
            if content:
                lines.append(f"{role}: {content}")
            plan = item.get("metadata", {}).get("plan")
            if plan:
                lines.append(
                    "上一轮计划: "
                    + json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
                )

        return "\n".join(lines) or "无历史消息。"


class ThinkTagStreamParser:
    start_tag = "<think>"
    end_tag = "</think>"

    def __init__(self) -> None:
        self.buffer = ""
        self.in_think = False

    def feed(self, content: str) -> list[dict[str, str]]:
        self.buffer += content
        events: list[dict[str, str]] = []

        while self.buffer:
            if not self.in_think:
                start_index = self.buffer.find(self.start_tag)
                stray_end_index = self.buffer.find(self.end_tag)
                if stray_end_index != -1 and (
                    start_index == -1 or stray_end_index < start_index
                ):
                    self.buffer = self.buffer[stray_end_index + len(self.end_tag) :]
                    continue

            tag = self.end_tag if self.in_think else self.start_tag
            tag_index = self.buffer.find(tag)

            if tag_index == -1:
                keep_length = self._partial_tag_length(self.buffer, tag)
                flush_length = len(self.buffer) - keep_length
                if flush_length <= 0:
                    break

                text = self.buffer[:flush_length]
                self.buffer = self.buffer[flush_length:]
                events.append(self._event(text))
                break

            text = self.buffer[:tag_index]
            if text:
                events.append(self._event(text))

            self.buffer = self.buffer[tag_index + len(tag) :]
            self.in_think = not self.in_think

        return [event for event in events if event["content"]]

    def flush(self) -> list[dict[str, str]]:
        if not self.buffer:
            return []

        event = self._event(self.buffer)
        self.buffer = ""
        return [event] if event["content"] else []

    def _event(self, content: str) -> dict[str, str]:
        return {
            "type": "thinking" if self.in_think else "delta",
            "content": content,
        }

    def _partial_tag_length(self, text: str, tag: str) -> int:
        max_length = min(len(text), len(tag) - 1)
        for length in range(max_length, 0, -1):
            if text[-length:] == tag[:length]:
                return length
        return 0
