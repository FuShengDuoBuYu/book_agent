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
from app.tools.family_orders import search_family_orders
from app.tools.personal_orders import search_personal_orders


class BookAgent:
    def __init__(self) -> None:
        # BookAgent 是总编排器：接用户输入、调 planner、调工具、再把结果交给模型生成答案。
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
        mode: str = "个人版",
        family_id: str | None = None,
    ) -> tuple[str, str | None]:
        chunks: list[str] = []
        resolved_session_id: str | None = None
        async for event in self.stream_chat(
            phone_num=phone_num,
            message=message,
            session_id=session_id,
            mode=mode,
            family_id=family_id,
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
        mode: str = "个人版",
        family_id: str | None = None,
    ) -> AsyncIterator[dict[str, str]]:
        # stream_chat 是整个 Agent 的主流程。
        # 前端看到的 status / thinking / delta / done 事件，都是从这里连续产出的。
        resolved_session_id: str | None = None
        assistant_chunks: list[str] = []
        try:
            book_mode = self._normalize_mode(mode)
            family_id = str(family_id or "").strip()
            yield {"type": "status", "content": "正在解析你的问题"}
            yield {"type": "status", "content": f"当前账本模式：{book_mode}"}
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
            plan = await self._create_plan(
                message=message, history=history_text, book_mode=book_mode
            )
            yield {
                "type": "status",
                "content": f"查询计划：{self._scope_text(plan.summary, book_mode)}",
            }

            if plan.needsTools:
                for tool_call in plan.toolCalls:
                    yield {
                        "type": "status",
                        "content": (
                            f"准备调用工具：{tool_call.id}，"
                            f"{self._format_tool_call(tool_call, book_mode)}，"
                            f"分类={tool_call.args.cost_type or '不限'}"
                        ),
                    }
                orders_json = await self._execute_tool_calls(
                    phone_num=phone_num,
                    phone_tail=self._phone_tail(phone_num),
                    mode=book_mode,
                    family_id=family_id,
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
                # 不需要查账单时，也统一给后续模型一个结构稳定的空结果。
                orders_json = json.dumps(
                    {
                        "ok": True,
                        "message": "本轮无需查询账单。",
                        "orderCount": 0,
                    },
                    ensure_ascii=False,
                )
            self._assert_no_raw_orders_for_answer(orders_json)
            yield {"type": "status", "content": "正在生成分析结果"}

            deterministic_reply = self._build_deterministic_reply(
                plan=plan,
                orders_json=orders_json,
            )
            if deterministic_reply:
                assistant_chunks.append(deterministic_reply)
                yield {"type": "delta", "content": deterministic_reply}
                await self._save_assistant_reply(
                    phone_num=phone_num,
                    session_id=resolved_session_id,
                    reply=deterministic_reply,
                    plan=plan,
                )
                yield {"type": "done", "content": ""}
                return

            # ThinkTagStreamParser 把模型流式输出拆成“思考”和“最终回答”两类事件。
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
            await self._save_assistant_reply(
                phone_num=phone_num,
                session_id=resolved_session_id,
                reply=assistant_reply,
                plan=plan,
            )

            yield {"type": "done", "content": ""}
        except Exception as exc:
            # 这里把异常转换成前端可消费的 error 事件，而不是直接让流中断。
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

    async def _save_assistant_reply(
        self,
        phone_num: str,
        session_id: str | None,
        reply: str,
        plan: AgentPlan,
    ) -> None:
        if not reply or not session_id:
            return
        await self.memory.add_message(
            phone_num=phone_num,
            session_id=session_id,
            role="assistant",
            content=reply,
            metadata={"plan": plan.model_dump(mode="json")},
        )

    @traceable(name="book_agent_create_plan", run_type="chain")
    async def _create_plan(
        self, message: str, history: str, book_mode: str
    ) -> AgentPlan:
        return await self.planner.create_plan(
            message=message,
            history=history,
            book_mode=book_mode,
        )

    @traceable(
        name="book_agent_execute_tool_calls",
        run_type="chain",
        process_inputs=lambda inputs: BookAgent._mask_trace_inputs(inputs),
    )
    async def _execute_tool_calls(
        self,
        phone_num: str,
        phone_tail: str,
        mode: str,
        family_id: str | None,
        plan: AgentPlan,
    ) -> str:
        # 一个 plan 里可能包含多个 tool call，例如“对比上月和本月”会产生两次查询。
        tool_results = []
        total_order_count = 0
        book_mode = self._normalize_mode(mode)
        family_id = str(family_id or "").strip()

        for tool_call in plan.toolCalls:
            raw_result = await self._execute_tool_call(
                phone_num=phone_num,
                tool_call=tool_call,
                mode=book_mode,
                family_id=family_id,
            )
            parsed_result = self._safe_json_loads(raw_result)
            order_count = int(parsed_result.get("orderCount") or 0)
            total_order_count += order_count
            # 查询工具返回原始账单，统计工具再把原始账单加工成适合回答的摘要信息。
            calculation_tool_results = self._calculate_orders(parsed_result, plan)
            user_info_count = len(parsed_result.get("userInfo") or [])
            tool_results.append(
                {
                    "toolCallId": tool_call.id,
                    "toolName": tool_call.toolName,
                    "reason": tool_call.reason,
                    "args": tool_call.args.model_dump(mode="json"),
                    "summary": self._format_tool_call(tool_call, book_mode),
                    "mode": parsed_result.get("mode", book_mode),
                    "ok": parsed_result.get("ok", False),
                    "error": parsed_result.get("error"),
                    "message": parsed_result.get("message"),
                    "familyIdSet": bool(parsed_result.get("familyIdSet")),
                    "familyIdResolved": bool(parsed_result.get("familyIdResolved")),
                    "orderCount": order_count,
                    "userFound": user_info_count > 0,
                    "query": self._safe_query_metadata(parsed_result.get("query", {})),
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
                "planSummary": self._scope_text(plan.summary, book_mode),
                "analysisType": plan.analysisType,
                "mode": book_mode,
                "familyIdSet": bool(family_id)
                or any(item.get("familyIdSet") for item in tool_results),
                "finalInstruction": plan.finalInstruction,
                "totalOrderCount": total_order_count,
                "toolResults": tool_results,
                "comparisonToolResult": comparison_tool_result,
            },
            ensure_ascii=False,
            default=str,
        )

    async def _execute_tool_call(
        self,
        phone_num: str,
        tool_call: ToolCall,
        mode: str,
        family_id: str | None,
    ) -> str:
        # Planner 只负责决定“查什么”，真正执行时才把当前身份和账本模式注入进去。
        if tool_call.toolName not in {"search_personal_orders", "search_family_orders"}:
            return json.dumps(
                {
                    "ok": False,
                    "error": "unsupported_tool",
                    "toolName": tool_call.toolName,
                },
                ensure_ascii=False,
            )

        args = tool_call.args
        if tool_call.toolName == "search_family_orders":
            return await search_family_orders(
                phone_num=phone_num,
                family_id=family_id or "",
                year=args.year,
                month=args.month,
                day=args.day,
                cost_type=args.cost_type,
                remark=args.remark,
            )

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

    def _build_deterministic_reply(
        self,
        plan: AgentPlan,
        orders_json: str,
    ) -> str | None:
        data = self._safe_json_loads(orders_json)
        if not data.get("ok", False):
            return None
        if not plan.needsTools:
            return None

        tool_results = data.get("toolResults") or []
        if not tool_results:
            return None
        if any(item.get("error") == "missing_family_id" for item in tool_results):
            return "家庭版查询需要 familyId，但当前用户资料里没有找到家庭 ID。请确认该账号已经创建或加入家庭。"
        if all(not item.get("userFound", False) for item in tool_results):
            return "当前 App 传入的用户身份没有在账单后端找到。"
        if all(int(item.get("orderCount") or 0) == 0 for item in tool_results):
            summaries = [
                str(item.get("summary") or "当前查询范围")
                for item in tool_results
                if item.get("userFound", False)
            ]
            scope = "、".join(summaries) or "当前查询范围"
            return f"{scope}没有查到账单记录。"

        return None

    def _safe_query_metadata(self, query: dict[str, Any]) -> dict[str, Any]:
        allowed_keys = {
            "mode",
            "year",
            "month",
            "day",
            "searchOrderRemark",
            "searchCostType",
            "ifIgnoreYear",
            "ifIgnoreMonth",
            "ifIgnoreDay",
        }
        return {key: value for key, value in query.items() if key in allowed_keys}

    def _assert_no_raw_orders_for_answer(self, orders_json: str) -> None:
        data = self._safe_json_loads(orders_json)
        if self._contains_raw_orders(data):
            raise RuntimeError("内部错误：最终回答 payload 中包含原始账单明细。")

    def _contains_raw_orders(self, value: Any) -> bool:
        if isinstance(value, dict):
            if "ordersInfo" in value or "result" in value:
                return True
            return any(self._contains_raw_orders(item) for item in value.values())
        if isinstance(value, list):
            return any(self._contains_raw_orders(item) for item in value)
        return False

    def _phone_tail(self, phone_num: str) -> str:
        value = str(phone_num or "")
        return value[-4:] if value else ""

    def _normalize_mode(self, value: str | None) -> str:
        mode = str(value or "").strip()
        if mode in {"家庭版", "家庭", "family", "family_mode"}:
            return "家庭版"
        return "个人版"

    def _scope_text(self, text: str, mode: str) -> str:
        if self._normalize_mode(mode) != "家庭版":
            return text
        return str(text or "").replace("个人账单", "家庭账单")

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
        if "family_id" in cleaned and cleaned["family_id"]:
            cleaned["family_id"] = "***"
        return cleaned

    def _safe_json_loads(self, value: str) -> dict:
        # 工具层当前通过 JSON 字符串传递结果，这里统一兜底，避免单个工具异常把整个链路炸掉。
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            return {"ok": False, "error": "invalid_tool_json", "raw": value}
        return data if isinstance(data, dict) else {"ok": False, "raw": data}

    def _format_tool_call(self, tool_call: ToolCall, mode: str = "个人版") -> str:
        args = tool_call.args
        scope = self._normalize_mode(mode).replace("版", "")
        if args.year is None:
            if args.month is not None and args.day is not None:
                return f"{args.month}月{args.day}日{scope}账单（不限年份）"
            if args.month is not None:
                return f"{args.month}月{scope}账单（不限年份）"
            return f"全部{scope}账单"
        if args.month is None:
            return f"{args.year}年{scope}账单"
        if args.day is None:
            return f"{args.year}年{args.month}月{scope}账单"
        return f"{args.year}年{args.month}月{args.day}日{scope}账单"

    def _format_history(self, messages: list[dict]) -> str:
        if not messages:
            return "无历史消息。"

        lines = []
        for item in messages:
            role = "用户" if item.get("role") == "user" else "助手"
            content = str(item.get("content", "")).strip()
            if content:
                lines.append(f"{role}: {self._sanitize_history_content(content)}")
            plan = item.get("metadata", {}).get("plan")
            if plan:
                lines.append(
                    "上一轮计划: "
                    + json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
                )

        return "\n".join(lines) or "无历史消息。"

    def _sanitize_history_content(self, content: str) -> str:
        markers = [
            '"toolResults"',
            "toolResults",
            '"ordersInfo"',
            "ordersInfo",
            '"calculationToolResults"',
            "calculationToolResults",
            '"comparisonToolResult"',
            "comparisonToolResult",
        ]
        cleaned = content
        for marker in markers:
            marker_index = cleaned.find(marker)
            if marker_index != -1:
                cleaned = cleaned[:marker_index].strip()
        return cleaned[:600]


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
