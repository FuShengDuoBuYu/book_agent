import asyncio
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.prompts import ChatPromptTemplate
from pydantic import ValidationError

from app.core.config import get_settings
from app.core.llm import get_planner_model
from app.schemas.plan import AgentPlan, SearchPersonalOrdersArgs, ToolCall


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
      "toolName": "search_personal_orders",
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

工具说明:
- search_personal_orders: 查询当前 App 用户的个人账单。Planner 不允许输出 phoneNum，Executor 会自动注入当前用户身份。
- year/month/day 都是可选过滤条件。查询整月时 day=null；查询整年时 month=null 且 day=null；查询全部时 year/month/day 都为 null。

规划规则:
- 当前只支持个人账单，不规划家庭账单。
- 账单分析、花费、收入、支出、分类、最高开销、总额、趋势，都需要 needsTools=true。
- 普通闲聊或询问能力介绍，needsTools=false，toolCalls=[]。
- 如果用户要求“对比”“比较”“环比”或同时提到多个时间范围，analysisType="comparison"，并为每个时间范围生成一个 toolCall。
- 如果用户只问一个时间范围，只生成一个 toolCall。
- 如果用户说“那上个月呢”“那前天呢”“继续”等追问，要结合会话历史沿用上一轮的分析类型和上下文。
- 如果用户没有说时间，默认使用当前年月。
- 如果用户说“这个月/本月”，使用当前年月。
- 如果用户说“上个月/上月”，使用当前日期的上一个自然月。
- 如果用户说“昨天/前天/今天”，生成 day 级别查询。
- 如果用户只说“3月”“4月”这类月份，没有说年份，year=null，month=对应月份。Executor 会在用户账本里按月份过滤。
- 不要输出 null 以外的字符串数字。
""".strip()


PLANNER_HUMAN_PROMPT = """
当前日期: {current_date}

最近会话历史:
{history}

用户最新问题:
{message}
""".strip()


class QueryPlanner:
    def __init__(self) -> None:
        self.prompt = ChatPromptTemplate.from_messages(
            [
                ("system", PLANNER_SYSTEM_PROMPT),
                ("human", PLANNER_HUMAN_PROMPT),
            ]
        )
        self.chain = self.prompt | get_planner_model()

    async def create_plan(self, message: str, history: str) -> AgentPlan:
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        try:
            response = await asyncio.wait_for(
                self.chain.ainvoke(
                    {
                        "current_date": now.strftime("%Y-%m-%d"),
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
            plan = self._fallback_plan(now)

        return self._normalize_plan(plan, now, message)

    def _extract_json(self, content: str) -> dict:
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

    def _normalize_plan(self, plan: AgentPlan, now: datetime, message: str) -> AgentPlan:
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
            plan.toolCalls = [self._default_month_tool_call(now)]

        normalized_calls = [
            self._normalize_tool_call(tool_call, index, now, message)
            for index, tool_call in enumerate(plan.toolCalls, start=1)
        ]
        plan.toolCalls = self._dedupe_tool_calls(normalized_calls)

        if not plan.summary or plan.summary == "本次账单分析计划":
            plan.summary = self._build_summary(plan)
        if not plan.finalInstruction:
            plan.finalInstruction = "根据所有工具结果回答用户问题。"

        return plan

    def _normalize_tool_call(
        self, tool_call: ToolCall, index: int, now: datetime, message: str
    ) -> ToolCall:
        args = tool_call.args
        args.cost_type = args.cost_type or "不限"
        args.remark = args.remark or ""

        if args.year is None and args.month is None and args.day is None:
            args.year = now.year
            args.month = now.month
        elif args.day is not None:
            args.year = args.year or now.year
            args.month = args.month or now.month
        elif args.month is not None:
            args.year = args.year

        if self._should_use_month_without_year(message, args.month):
            args.year = None

        tool_call.args = args
        if not tool_call.id:
            tool_call.id = f"search_orders_{index}"
        if not tool_call.reason:
            tool_call.reason = self._format_tool_call(tool_call)
        return tool_call

    def _dedupe_tool_calls(self, tool_calls: list[ToolCall]) -> list[ToolCall]:
        unique_calls: list[ToolCall] = []
        seen: set[tuple[int | None, int | None, int | None, str, str]] = set()
        for tool_call in tool_calls:
            args = tool_call.args
            key = (args.year, args.month, args.day, args.cost_type, args.remark)
            if key in seen:
                continue
            seen.add(key)
            unique_calls.append(tool_call)
        return unique_calls

    def _fallback_plan(self, now: datetime) -> AgentPlan:
        return AgentPlan(
            intent="analyze_orders",
            analysisType="general_summary",
            needsTools=True,
            toolCalls=[self._default_month_tool_call(now)],
            finalInstruction="根据默认查询到的本月账单回答用户问题。",
            summary=f"{now.year}年{now.month}月个人账单",
            reason="Planner 输出无法解析，使用默认本月账单查询。",
        )

    def _default_month_tool_call(self, now: datetime) -> ToolCall:
        return ToolCall(
            id="search_current_month_orders",
            toolName="search_personal_orders",
            args=SearchPersonalOrdersArgs(year=now.year, month=now.month, day=None),
            reason="用户问题需要账单数据，默认查询本月个人账单。",
        )

    def _build_summary(self, plan: AgentPlan) -> str:
        summaries = [self._format_tool_call(tool_call) for tool_call in plan.toolCalls]
        if len(summaries) > 1:
            return "、".join(summaries) + "后对比分析"
        return summaries[0] if summaries else "无需查询账单"

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

    def _strip_think_tags(self, content: str) -> str:
        without_blocks = re.sub(
            r"<think>.*?</think>",
            "",
            content,
            flags=re.DOTALL | re.IGNORECASE,
        )
        return re.sub(r"</?think>", "", without_blocks, flags=re.IGNORECASE)

    def _should_use_month_without_year(
        self, message: str, month: int | None
    ) -> bool:
        if month is None:
            return False

        text = message.strip()
        if re.search(r"\d{4}\s*年", text):
            return False
        if any(keyword in text for keyword in ["今年", "本年", "去年", "前年"]):
            return False
        if any(keyword in text for keyword in ["这个月", "本月", "当月", "上个月", "上月"]):
            return False

        month_patterns = [
            rf"(?<!\d){month}\s*月(?:份)?",
            rf"(?<!\d){month:02d}\s*月(?:份)?",
        ]
        return any(re.search(pattern, text) for pattern in month_patterns)
