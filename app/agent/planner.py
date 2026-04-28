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
from app.schemas.plan import AgentPlan, SearchPersonalOrdersArgs, ToolCall
from app.tools.registry import (
    default_tool_name_for_mode,
    planner_tool_names,
    render_planner_tools_for_prompt,
)


PLANNER_SYSTEM_PROMPT = """
/no_think

你是记账 Agent 的 Planner。你的任务不是回答用户，而是把用户问题和会话历史转换为可执行 JSON 计划。

你必须只输出 JSON，不要输出 Markdown，不要输出解释，不要输出 <think> 标签。

计划 JSON schema:
{{
  "intent": "analyze_orders" | "chat" | "unknown",
  "analysisType": "highest_expense" | "category_expense" | "total_spending" | "income_expense" | "comparison" | "general_summary" | "chat" | "unknown",
  "needsTools": true | false,
  "toolCalls": [
    {{
      "id": "短的英文或拼音步骤 id",
      "toolName": "search_personal_orders" | "search_family_orders",
      "args": {{
        "year": number | null,
        "month": number | null,
        "day": number | null,
        "cost_type": "不限",
        "remark": ""
      }},
      "reason": "一句中文说明为什么调用这个工具"
    }}
  ],
  "finalInstruction": "一句中文说明最终回答应该怎么综合工具结果",
  "followUpOfPreviousQuestion": true | false,
  "summary": "一句中文计划摘要",
  "reason": "一句中文说明为什么这么计划"
}}

可用工具列表:
{tools_text}

工具选择要求:
- toolName 必须从可用工具列表里选择，并由你根据用户语义决定，程序不会根据当前账本模式替你改 toolName。
- args 只能包含 year、month、day、cost_type、remark；不要输出 phoneNum、phone_num、familyId、family_id、userId。
- 当前账本模式只是默认上下文：如果用户没有明确说个人或家庭，就使用当前账本模式对应的工具。
- 如果用户明确问个人账单、我自己的账单、我的消费，使用 search_personal_orders。
- 如果用户明确问家庭账单、家里、全家、家庭总额，使用 search_family_orders。
- 账单分析、花费、收入、支出、分类、最高开销、总额、趋势，都需要 needsTools=true。
- 普通闲聊或询问能力介绍，needsTools=false，toolCalls=[]。
- 如果用户要求“对比”“比较”“环比”或同时提到多个时间范围，analysisType="comparison"，并为每个时间范围生成一个 toolCall。
- 如果用户只问一个时间范围，只生成一个 toolCall。
- 如果用户说“那上个月呢”“那前天呢”“继续”等追问，要结合会话历史沿用上一轮的分析类型和上下文。
- 如果用户没有说时间，默认使用当前年月。
- 如果用户说“这个月/本月”，使用当前年月。
- 如果用户说“上个月/上月”，使用当前日期的上一个自然月。
- 如果用户说“昨天/前天/今天”，生成 day 级别查询。
- 如果用户只说“3月”“4月”这类月份，没有说年份，使用当前年份。
- 只有用户明确说“不限年份/所有年份/全部年份/历年”时，才输出 year=null。
- 不要输出 null 以外的字符串数字。
""".strip()


PLANNER_HUMAN_PROMPT = """
当前日期: {current_date}

当前账本模式: {book_mode}

最近会话历史:
{history}

用户最新问题:
{message}
""".strip()


class QueryPlanner:
    def __init__(self) -> None:
        # Planner 不直接回答用户，它只把自然语言问题翻译成结构化执行计划。
        self.prompt = ChatPromptTemplate.from_messages(
            [
                ("system", PLANNER_SYSTEM_PROMPT),
                ("human", PLANNER_HUMAN_PROMPT),
            ]
        ).partial(tools_text=render_planner_tools_for_prompt())
        self.chain = self.prompt | get_planner_model()
        self.allowed_tool_names = set(planner_tool_names())

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
            plan = self._fallback_plan(now, book_mode)

        return self._normalize_plan(plan, now, message, book_mode)

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
        self, plan: AgentPlan, now: datetime, message: str, book_mode: str
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
            plan.toolCalls = [self._default_month_tool_call(now, book_mode)]

        normalized_calls = [
            self._normalize_tool_call(tool_call, index, now, message)
            for index, tool_call in enumerate(plan.toolCalls, start=1)
        ]
        plan.toolCalls = self._dedupe_tool_calls(normalized_calls)

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
        if tool_call.toolName not in self.allowed_tool_names:
            tool_call.toolName = default_tool_name_for_mode("个人版")
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
        return unique_calls

    def _fallback_plan(self, now: datetime, book_mode: str) -> AgentPlan:
        return AgentPlan(
            intent="analyze_orders",
            analysisType="general_summary",
            needsTools=True,
            toolCalls=[self._default_month_tool_call(now, book_mode)],
            finalInstruction="根据默认查询到的本月账单回答用户问题。",
            summary=f"{now.year}年{now.month}月{self._scope_name(book_mode)}账单",
            reason="Planner 输出无法解析，使用默认本月账单查询。",
        )

    def _default_month_tool_call(self, now: datetime, book_mode: str) -> ToolCall:
        return ToolCall(
            id="search_current_month_orders",
            toolName=default_tool_name_for_mode(book_mode),
            args=SearchPersonalOrdersArgs(year=now.year, month=now.month, day=None),
            reason=f"用户问题需要账单数据，默认查询本月{self._scope_name(book_mode)}账单。",
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

    def _scope_name(self, book_mode: str) -> str:
        return "家庭" if self._normalize_mode(book_mode) == "家庭版" else "个人"

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
