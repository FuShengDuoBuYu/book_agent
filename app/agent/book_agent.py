import json
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import datetime
from time import perf_counter
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.prompts import ChatPromptTemplate
from langsmith import traceable

from app.agent.planner import QueryPlanner
from app.agent.prompts import BOOK_AGENT_ANALYSIS_PROMPT, BOOK_AGENT_SYSTEM_PROMPT
from app.clients.bookkeeping_api import BookkeepingApiClient
from app.core.llm import get_chat_model
from app.core.config import get_settings
from app.memory.store import MongoMemoryStore, conversation_scope
from app.schemas.plan import AgentPlan, ToolCall
from app.tools.order_calculations import (
    calculate_category_breakdown,
    calculate_order_summary,
    compare_periods,
    find_top_expenses,
    find_top_incomes,
)
from app.tools.order_search import search_orders
from app.tools.registry import PLANNER_TOOL_BY_NAME, format_tool_call
from app.tools.financial_analysis import _date, merge_orders, run_analysis

logger = logging.getLogger(__name__)


class BookAgent:
    def __init__(self, ledger_client: BookkeepingApiClient) -> None:
        self.ledger_client = ledger_client
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
        new_session: bool = False,
    ) -> tuple[str, str | None]:
        chunks: list[str] = []
        resolved_session_id: str | None = None
        async for event in self.stream_chat(
            phone_num=phone_num,
            message=message,
            session_id=session_id,
            mode=mode,
            family_id=family_id,
            new_session=new_session,
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
        new_session: bool = False,
    ) -> AsyncIterator[dict[str, str]]:
        # stream_chat 是整个 Agent 的主流程。
        # 前端看到的 status / thinking / delta / done 事件，都是从这里连续产出的。
        resolved_session_id: str | None = None
        assistant_chunks: list[str] = []
        started_at = perf_counter()
        try:
            book_mode = self._normalize_mode(mode)
            family_id = str(family_id or "").strip()
            book_scope = conversation_scope(book_mode, family_id)
            yield {"type": "status", "content": "正在解析你的问题"}
            yield {"type": "status", "content": f"当前账本模式：{book_mode}"}
            resolved_session_id = await self.memory.ensure_session(
                phone_num=phone_num,
                session_id=session_id,
                first_message=message,
                book_scope=book_scope,
                new_session=new_session,
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
                "content": f"查询计划：{plan.summary}",
            }

            if plan.needsTools:
                for tool_call in plan.toolCalls:
                    yield {
                        "type": "status",
                        "content": (
                            f"准备调用工具：{tool_call.id}，"
                            f"{format_tool_call(tool_call)}，"
                            f"分类={tool_call.args.cost_type or '不限'}"
                        ),
                    }
                orders_json = await self._execute_tool_calls(
                    mode=book_mode,
                    family_id=family_id,
                    plan=plan,
                )
                yield {"type": "status", "content": self._query_status(orders_json)}
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
                    evidence=self._safe_json_loads(orders_json),
                    duration_ms=round((perf_counter() - started_at) * 1000),
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
                    "plan_json": json.dumps({"analyses": [task.model_dump() for task in plan.analyses],
                                             "analysisWindow": plan.analysisWindow.model_dump(mode="json") if plan.analysisWindow else None},
                                            ensure_ascii=False) if plan.analyses else plan.model_dump_json(),
                    "orders_json": self._answer_evidence(orders_json),
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
                evidence=self._safe_json_loads(orders_json),
                duration_ms=round((perf_counter() - started_at) * 1000),
            )

            yield {"type": "done", "content": ""}
        except Exception as exc:
            logger.warning("AI bill chat failed (%s)", type(exc).__name__)
            yield {"type": "error", "content": "AI 账单处理失败，请稍后重试。"}

    def _query_status(self, orders_json: str) -> str:
        data = self._safe_json_loads(orders_json)
        results = data.get("toolResults") or []
        if any(not result.get("ok") for result in results):
            return "账单查询未完成"
        return f"查询完成，共获取 {data.get('totalOrderCount', 0)} 条账单"

    def _answer_evidence(self, orders_json: str) -> str:
        data = self._safe_json_loads(orders_json)
        if data.get("analysisResults"):
            # Present one coherent analysis, without duplicate per-query amounts.
            data = {"analysisResults": data["analysisResults"]}
        return json.dumps(data, ensure_ascii=False, default=str)

    async def _save_assistant_reply(
        self,
        phone_num: str,
        session_id: str | None,
        reply: str,
        plan: AgentPlan,
        evidence: dict | None = None,
        duration_ms: int | None = None,
    ) -> None:
        if not reply or not session_id:
            return
        settings = get_settings()
        await self.memory.add_message(
            phone_num=phone_num,
            session_id=session_id,
            role="assistant",
            content=reply,
            metadata={"plan": plan.model_dump(mode="json"), "evidence": evidence or {},
                      "analysisVersion": 2, "durationMs": duration_ms,
                      "model": {"provider": settings.llm_provider, "name": settings.llm_model,
                                "reasoning": settings.llm_reasoning,
                                "reasoningEffort": settings.llm_reasoning_effort}},
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
        mode: str,
        family_id: str | None,
        plan: AgentPlan,
    ) -> str:
        # 一个 plan 里可能包含多个 tool call，例如“对比上月和本月”会产生两次查询。
        tool_results = []
        datasets = defaultdict(list)
        total_order_count = 0
        book_mode = self._normalize_mode(mode)
        family_id = str(family_id or "").strip()

        for tool_call in plan.toolCalls:
            parsed_result = await self._execute_tool_call(tool_call, family_id)
            if parsed_result.get("ok") and plan.analysisWindow and not plan.analyses:
                window = plan.analysisWindow
                records = [order for order in parsed_result.get("ordersInfo", [])
                           if _date(order) and window.startDate <= _date(order) <= window.endDate]
                parsed_result = {**parsed_result, "ordersInfo": records, "orderCount": len(records)}
            if parsed_result.get("ok"):
                datasets[parsed_result.get("mode", book_mode)].append((tool_call, parsed_result.get("ordersInfo", [])))
            order_count = int(parsed_result.get("orderCount") or 0)
            total_order_count += order_count
            # 查询工具返回原始账单，统计工具再把原始账单加工成适合回答的摘要信息。
            calculation_tool_results = (
                self._calculate_orders(parsed_result, plan) if parsed_result.get("ok") else {}
            )
            tool_results.append(
                {
                    "toolCallId": tool_call.id,
                    "toolName": tool_call.toolName,
                    "reason": tool_call.reason,
                    "args": tool_call.args.model_dump(mode="json"),
                    "summary": format_tool_call(tool_call),
                    "mode": parsed_result.get("mode", book_mode),
                    "ok": parsed_result.get("ok", False),
                    "error": parsed_result.get("error"),
                    "message": parsed_result.get("message"),
                    "orderCount": order_count,
                    "userFound": bool(parsed_result.get("userFound")),
                    "query": parsed_result.get("query", {}),
                    "localFilter": parsed_result.get("localFilter", {}),
                    "normalizedCostType": parsed_result.get("normalizedCostType", "不限"),
                    "calculationToolResults": calculation_tool_results,
                }
            )

        comparison_tool_result = None
        if len(tool_results) > 1 and all(item["ok"] for item in tool_results):
            comparison_tool_result = self._compare_tool_results(tool_results)

        analysis_results = []
        if plan.analyses and all(item["ok"] for item in tool_results):
            today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
            for scope, queries in datasets.items():
                for task in plan.analyses:
                    bound = [(call, rows) for call, rows in queries
                             if not task.queryIds or set(task.queryIds) & set(call.sourceIds or [call.id])]
                    if not bound:
                        continue
                    filters = {(call.args.cost_type, call.args.remark) for call, _ in bound}
                    if len(filters) > 1 and not task.queryIds:
                        analysis_results.append({"mode": scope, "tool": task.tool, "evidence": {
                            "sufficientData": False, "reason": "该分析尚未明确关联哪个分类或备注范围，需要确认查询范围。"}})
                        continue
                    records = merge_orders([rows for _, rows in bound])
                    window = plan.analysisWindow if task.tool not in {"forecast", "scenario", "savings", "budget"} else None
                    selected = records
                    if window:
                        selected = [order for order in records if _date(order) and window.startDate <= _date(order) <= window.endDate]
                    result = run_analysis(
                        task.tool, selected, as_of=today, metric=task.metric,
                        target_month=task.targetMonth,
                        changes=[change.model_dump() for change in task.changes],
                        budget_amount=task.budgetAmount, starting_balance=task.startingBalance,
                        target_amount=task.targetAmount, months=task.months,
                        window_start=window.startDate if window else None,
                        window_end=window.endDate if window else None,
                    )
                    analysis_results.append({"mode": scope, "tool": task.tool, "queryIds": task.queryIds,
                                             "filters": [{"category": category, "remark": remark} for category, remark in sorted(filters)],
                                             "evidence": result})

        return json.dumps(
            {
                "ok": True,
                "message": "账单查询完成。",
                "planSummary": plan.summary,
                "analysisType": plan.analysisType,
                "mode": book_mode,
                "familyIdSet": bool(family_id),
                "finalInstruction": plan.finalInstruction,
                "totalOrderCount": total_order_count,
                "toolResults": tool_results,
                "comparisonToolResult": comparison_tool_result,
                "analysisResults": analysis_results,
            },
            ensure_ascii=False,
            default=str,
        )

    async def _execute_tool_call(self, tool_call: ToolCall, family_id: str) -> dict:
        tool = PLANNER_TOOL_BY_NAME.get(tool_call.toolName)
        if tool is None:
            return {"ok": False, "error": "unsupported_tool"}

        mode, _ = tool
        family_scope = family_id if mode == "家庭版" else ""
        if mode == "家庭版" and not family_scope:
            return {"ok": False, "error": "family_not_available"}

        return await search_orders(
            self.ledger_client, mode, family_scope=family_scope, **tool_call.args.model_dump()
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
        if plan.intent == "unknown":
            return "这次未能生成可执行的分析计划，请补充你想分析的范围或换一种表述后重试。"
        if not data.get("ok", False):
            return None
        if not plan.needsTools:
            return None

        tool_results = data.get("toolResults") or []
        if not tool_results:
            return None
        if any(item.get("error") == "family_not_available" for item in tool_results):
            return "当前账号未加入家庭账本。"
        if any(item.get("error") == "bookkeeping_access_denied" for item in tool_results):
            return "账本权限或会话已变化，请从自动记账重新进入。"
        if any(not item.get("ok") for item in tool_results):
            return "账本服务暂不可用，本次查询未完成。"
        if all(not item.get("userFound", False) for item in tool_results):
            return "账本服务未返回当前账号的资料。"
        if all(int(item.get("orderCount") or 0) == 0 for item in tool_results):
            summaries = [
                str(item.get("summary") or "当前查询范围")
                for item in tool_results
                if item.get("userFound", False)
            ]
            scope = "、".join(summaries) or "当前查询范围"
            return f"{scope}没有查到账单记录。"

        analyses = data.get("analysisResults") or []
        if analyses and all(item.get("evidence", {}).get("sufficientData") is False for item in analyses):
            input_names = {"budgetAmount": "预算金额", "startingBalance": "当前存款余额", "targetAmount": "目标金额"}
            missing = list(dict.fromkeys(input_names.get(field, field) for item in analyses
                                        for field in item["evidence"].get("missingInputs", [])))
            if missing:
                return "账单已查询，但完成这次测算还需要确认：" + "、".join(missing) + "。请补充这些条件。"
            reasons = list(dict.fromkeys(item["evidence"].get("reason") or "可用的完整月份不足以支持可靠的推算。" for item in analyses))
            return "当前证据不足以完成这次分析。" + "；".join(reasons)

        return None

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

    def _normalize_mode(self, value: str | None) -> str:
        mode = str(value or "").strip()
        if mode in {"家庭版", "家庭", "family", "family_mode"}:
            return "家庭版"
        return "个人版"

    @staticmethod
    def _mask_trace_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
        cleaned = {key: value for key, value in inputs.items() if key != "self"}
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
