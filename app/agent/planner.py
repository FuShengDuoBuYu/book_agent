import asyncio
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.prompts import ChatPromptTemplate
from langsmith import traceable
from pydantic import ValidationError

from app.core.config import get_settings
from app.core.llm import get_planner_model
from app.agent.prompts import PLANNER_HUMAN_PROMPT, PLANNER_SYSTEM_PROMPT
from app.schemas.plan import (
    AgentPlan,
    ToolCall,
    render_analysis_types_for_prompt,
    render_intents_for_prompt,
)
from app.tools.registry import PLANNER_TOOL_BY_NAME, render_planner_tools_for_prompt


class QueryPlanner:
    def __init__(self) -> None:
        # Planner 不直接回答用户，它只把自然语言问题翻译成结构化执行计划。
        self.prompt = ChatPromptTemplate.from_messages(
            [
                ("system", PLANNER_SYSTEM_PROMPT),
                ("human", PLANNER_HUMAN_PROMPT),
            ]
        ).partial(
            tools_text=render_planner_tools_for_prompt(),
            intent_types_text=render_intents_for_prompt(),
            analysis_types_text=render_analysis_types_for_prompt(),
        )
        self.chain = self.prompt | get_planner_model()

    @traceable(name="query_planner", run_type="chain")
    async def create_plan(
        self, message: str, history: str, book_mode: str = "个人版"
    ) -> AgentPlan:
        # 这里的核心任务是：拿到 LLM 输出后，尽量把它收敛成一个可执行的 AgentPlan。
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        try:
            response = await asyncio.wait_for(
                self.chain.ainvoke(
                    {
                        "current_date": now.strftime("%Y-%m-%d"),
                        "book_mode": self._normalize_mode(book_mode),
                        "history": history,
                        "message": message,
                    }
                ),
                timeout=get_settings().planner_timeout_seconds,
            )
            raw_content = str(getattr(response, "content", "")).strip()
            data = self._extract_json(raw_content)
            plan = AgentPlan.model_validate(data)
        except (
            asyncio.TimeoutError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
        ):
            plan = self._fallback_plan()

        return self._normalize_plan(plan, now, message)

    def _extract_json(self, content: str) -> dict:
        # 模型有时会包一层 markdown code fence，或带 <think> 标签，这里先做清洗再取 JSON。
        content = self._strip_think_tags(content).strip()
        if content.startswith("```"):
            content = content.strip("`")
            if content.startswith("json"):
                content = content[4:].strip()

        start = content.find("{")
        end = content.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {}

        return json.loads(content[start : end + 1])

    def _normalize_plan(
        self, plan: AgentPlan, now: datetime, message: str
    ) -> AgentPlan:
        # normalize 的目的，是把“不稳定的 LLM 输出”整理成“稳定的程序输入”。
        if plan.intent != "analyze_orders":
            plan.needsTools = False
            plan.toolCalls = []
            if not plan.finalInstruction:
                plan.finalInstruction = "直接回答用户问题。"
            if not plan.summary or plan.summary == "本次账单分析计划":
                plan.summary = "无需查询账单的对话"
            return plan

        if not plan.needsTools:
            plan.toolCalls = []
            return plan

        if not plan.toolCalls:
            plan.needsTools = False
            plan.summary = "Planner 未选择工具"
            plan.finalInstruction = "说明当前无法查询账单，因为模型没有选择任何工具。"
            return plan

        normalized_calls = [
            self._normalize_tool_call(tool_call, index, now, message)
            for index, tool_call in enumerate(plan.toolCalls, start=1)
        ]
        plan.toolCalls = self._dedupe_tool_calls(normalized_calls)
        if not plan.toolCalls:
            plan.needsTools = False
            plan.summary = "Planner 没有选择合法工具"
            plan.finalInstruction = "说明当前无法查询账单，因为模型没有选择合法工具。"
            return plan

        plan.summary = self._build_summary(plan)
        if not plan.finalInstruction:
            plan.finalInstruction = "根据所有工具结果回答用户问题。"

        return plan

    def _normalize_tool_call(
        self,
        tool_call: ToolCall,
        index: int,
        now: datetime,
        message: str,
    ) -> ToolCall:
        args = tool_call.args
        args.cost_type = self._normalize_cost_type(args.cost_type)
        args.remark = args.remark or ""

        if args.year is None and args.month is None and args.day is None:
            args.year = now.year
            args.month = now.month
        elif args.day is not None:
            args.year = args.year or now.year
            args.month = args.month or now.month
        elif args.month is not None:
            args.year = args.year or now.year

        if self._should_use_month_without_year(message):
            args.year = None

        tool_call.args = args
        if not tool_call.id:
            tool_call.id = f"search_orders_{index}"
        if not tool_call.reason:
            tool_call.reason = self._format_tool_call(tool_call)
        return tool_call

    def _dedupe_tool_calls(self, tool_calls: list[ToolCall]) -> list[ToolCall]:
        # 对比类问题很容易让模型生成重复查询，这里根据参数去重。
        unique_calls: list[ToolCall] = []
        seen: set[tuple[str, int | None, int | None, int | None, str, str]] = set()
        for tool_call in tool_calls:
            args = tool_call.args
            key = (
                tool_call.toolName,
                args.year,
                args.month,
                args.day,
                args.cost_type,
                args.remark,
            )
            if key in seen:
                continue
            seen.add(key)
            unique_calls.append(tool_call)
        return [
            tool_call
            for tool_call in unique_calls
            if tool_call.toolName in PLANNER_TOOL_BY_NAME
        ]

    def _fallback_plan(self) -> AgentPlan:
        return AgentPlan(
            intent="unknown",
            analysisType="unknown",
            needsTools=False,
            toolCalls=[],
            finalInstruction="说明当前无法查询账单，因为 Planner 输出无法解析。",
            summary="Planner 输出无法解析",
            reason="模型没有产出可解析的结构化计划。",
        )

    def _build_summary(self, plan: AgentPlan) -> str:
        summaries = [self._format_tool_call(tool_call) for tool_call in plan.toolCalls]
        if len(summaries) > 1:
            return "、".join(summaries) + "后对比分析"
        return summaries[0] if summaries else "无需查询账单"

    def _format_tool_call(self, tool_call: ToolCall) -> str:
        args = tool_call.args
        scope = "家庭" if tool_call.toolName == "search_family_orders" else "个人"
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

    def _normalize_mode(self, value: str | None) -> str:
        mode = str(value or "").strip()
        if mode in {"家庭版", "家庭", "family", "family_mode"}:
            return "家庭版"
        return "个人版"

    def _strip_think_tags(self, content: str) -> str:
        without_blocks = re.sub(
            r"<think>.*?</think>",
            "",
            content,
            flags=re.DOTALL | re.IGNORECASE,
        )
        return re.sub(r"</?think>", "", without_blocks, flags=re.IGNORECASE)

    def _normalize_cost_type(self, value: str | None) -> str:
        cost_type = str(value or "").strip()
        if not cost_type:
            return "不限"

        broad_words = {
            "不限",
            "全部",
            "所有",
            "支出",
            "花费",
            "消费",
            "开销",
            "费用",
            "明细",
            "支出明细",
            "消费明细",
            "花费明细",
            "开销明细",
            "各分类",
            "分类",
        }
        return "不限" if cost_type in broad_words else cost_type

    def _should_use_month_without_year(self, message: str) -> bool:
        text = message.strip()
        return any(
            keyword in text
            for keyword in ["不限年份", "所有年份", "全部年份", "历年", "每一年"]
        )
