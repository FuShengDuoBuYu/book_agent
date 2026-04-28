import json
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.prompts import ChatPromptTemplate
from langsmith import traceable

from app.agent.planner import QueryPlanner
from app.agent.prompts import BOOK_AGENT_ANALYSIS_PROMPT, BOOK_AGENT_SYSTEM_PROMPT
from app.core.llm import get_chat_model
from app.memory.store import MongoMemoryStore
from app.schemas.plan import AgentPlan, ToolCall
from app.tools.order_calculations import (
    calculate_category_breakdown,
    calculate_order_summary,
    compare_periods,
    find_top_expenses,
    find_top_incomes,
)
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
            # Temporarily disable conversation history while debugging single-turn
            # agent traces in LangSmith.
            history_messages = []
            yield {"type": "status", "content": "已暂时关闭会话历史"}
            history_text = "无历史消息。"
            await self.memory.add_message(
                phone_num=phone_num,
                session_id=resolved_session_id,
                role="user",
                content=message,
            )

            yield {"type": "status", "content": "正在规划账单查询"}
            plan = await self._create_plan(message=message, history=history_text)
            yield {
                "type": "status",
                "content": f"查询计划：{plan.summary}",
            }

            if plan.needsTools:
                for tool_call in plan.toolCalls:
                    yield {
                        "type": "status",
                        "content": (
                            f"准备调用工具：{tool_call.id}，"
                            f"{self._format_tool_call(tool_call)}，"
                            f"分类={tool_call.args.cost_type or '不限'}"
                        ),
                    }
                orders_json = await self._execute_tool_calls(
                    phone_num=phone_num,
                    phone_tail=self._phone_tail(phone_num),
                    plan=plan,
                )
                order_count = self._extract_order_count(orders_json)
                user_match_summary = self._extract_user_match_summary(orders_json)
                yield {
                    "type": "status",
                    "content": (
                        f"已执行 {len(plan.toolCalls)} 个工具调用，"
                        f"共获取 {order_count} 条账单，{user_match_summary}"
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

    def _extract_user_match_summary(self, orders_json: str) -> str:
        try:
            data = json.loads(orders_json)
        except json.JSONDecodeError:
            return "用户身份状态未知"

        tool_results = data.get("toolResults") or []
        if not tool_results:
            return "用户身份状态未知"
        if all(item.get("userFound") for item in tool_results):
            return "用户身份已匹配"
        if any(item.get("userFound") for item in tool_results):
            return "部分工具调用匹配到用户身份"
        return "用户身份未匹配"

    @traceable(name="book_agent_create_plan", run_type="chain")
    async def _create_plan(self, message: str, history: str) -> AgentPlan:
        return await self.planner.create_plan(message=message, history=history)

    @traceable(
        name="book_agent_execute_tool_calls",
        run_type="chain",
        process_inputs=lambda inputs: BookAgent._mask_trace_inputs(inputs),
    )
    async def _execute_tool_calls(
        self,
        phone_num: str,
        phone_tail: str,
        plan: AgentPlan,
    ) -> str:
        tool_results = []
        total_order_count = 0

        for tool_call in plan.toolCalls:
            raw_result = await self._execute_tool_call(phone_num, tool_call)
            parsed_result = self._safe_json_loads(raw_result)
            order_count = int(parsed_result.get("orderCount") or 0)
            total_order_count += order_count
            calculation_tool_results = self._calculate_orders(parsed_result, plan)
            user_info_count = len(parsed_result.get("userInfo") or [])
            tool_results.append(
                {
                    "toolCallId": tool_call.id,
                    "toolName": tool_call.toolName,
                    "reason": tool_call.reason,
                    "args": tool_call.args.model_dump(mode="json"),
                    "summary": self._format_tool_call(tool_call),
                    "orderCount": order_count,
                    "userFound": user_info_count > 0,
                    "query": parsed_result.get("query", {}),
                    "localFilter": parsed_result.get("localFilter", {}),
                    "normalizedCostType": parsed_result.get("normalizedCostType", "不限"),
                    "calculationToolResults": calculation_tool_results,
                }
            )

        comparison_tool_result = None
        if len(tool_results) > 1:
            comparison_tool_result = self._compare_tool_results(tool_results)

        return json.dumps(
            {
                "ok": True,
                "message": "账单查询完成。",
                "planSummary": plan.summary,
                "analysisType": plan.analysisType,
                "finalInstruction": plan.finalInstruction,
                "totalOrderCount": total_order_count,
                "toolResults": tool_results,
                "comparisonToolResult": comparison_tool_result,
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

    def _calculate_orders(
        self, parsed_result: dict[str, Any], plan: AgentPlan
    ) -> dict[str, Any]:
        orders_json = json.dumps(parsed_result, ensure_ascii=False, default=str)
        analysis_type = plan.analysisType
        results: dict[str, Any] = {
            "summary": self._safe_json_loads(calculate_order_summary(orders_json))
        }

        if analysis_type in {"category_expense", "comparison", "general_summary"}:
            results["categoryBreakdown"] = self._safe_json_loads(
                calculate_category_breakdown(orders_json)
            )

        if analysis_type in {"highest_expense", "comparison", "general_summary"}:
            results["topExpenses"] = self._safe_json_loads(
                find_top_expenses(orders_json, top_n=5)
            )

        if analysis_type in {"income_expense", "general_summary"}:
            results["topIncomes"] = self._safe_json_loads(
                find_top_incomes(orders_json, top_n=5)
            )

        return results

    def _compare_tool_results(
        self, tool_results: list[dict[str, Any]]
    ) -> dict[str, Any]:
        periods = [
            {
                "label": item.get("summary", ""),
                "calculationToolResults": item.get("calculationToolResults", {}),
            }
            for item in tool_results
        ]
        raw_comparison = compare_periods(
            json.dumps(periods, ensure_ascii=False, default=str)
        )
        return self._safe_json_loads(raw_comparison)

    def _phone_tail(self, phone_num: str) -> str:
        value = str(phone_num or "")
        return value[-4:] if value else ""

    @staticmethod
    def _mask_phone(value: str) -> str:
        phone = str(value or "")
        if len(phone) <= 4:
            return phone
        return f"***{phone[-4:]}"

    @staticmethod
    def _mask_trace_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
        cleaned = {key: value for key, value in inputs.items() if key != "self"}
        if "phone_num" in cleaned:
            cleaned["phone_num"] = BookAgent._mask_phone(cleaned["phone_num"])
        return cleaned

    def _safe_json_loads(self, value: str) -> dict:
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            return {"ok": False, "error": "invalid_tool_json", "raw": value}
        return data if isinstance(data, dict) else {"ok": False, "raw": data}

    def _format_tool_call(self, tool_call: ToolCall) -> str:
        args = tool_call.args
        if args.year is None:
            if args.month is not None and args.day is not None:
                return f"{args.month}月{args.day}日个人账单（不限年份）"
            if args.month is not None:
                return f"{args.month}月个人账单（不限年份）"
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
