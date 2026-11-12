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
from app.agent.analysis_context import resolve_context
from app.agent.prompts import PLANNER_HUMAN_PROMPT, PLANNER_SYSTEM_PROMPT
from app.schemas.plan import (
    AgentPlan,
    AnalysisRequest,
    SearchPersonalOrdersArgs,
    ToolCall,
    render_analysis_types_for_prompt,
    render_intents_for_prompt,
)
from app.tools.registry import PLANNER_TOOL_BY_NAME, format_tool_call, render_planner_tools_for_prompt
from app.tools.financial_analysis import ANALYSIS_TOOLS, shift_month


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
            analysis_tools_text="\n".join(f"- {name}: {description}" for name, description in ANALYSIS_TOOLS.items()),
        )
        self.chain = self.prompt | get_planner_model()

    @traceable(name="query_planner", run_type="chain")
    async def create_plan(
        self, message: str, history: str, book_mode: str = "个人版", previous_context: dict | None = None
    ) -> AgentPlan:
        # 这里的核心任务是：拿到 LLM 输出后，尽量把它收敛成一个可执行的 AgentPlan。
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        prompt_values = {
            "current_date": now.strftime("%Y-%m-%d"),
            "book_mode": self._normalize_mode(book_mode),
            "history": history + '\n上轮已解析的用户条件（追问时沿用，独立新问题不沿用）：' + json.dumps(previous_context or {}, ensure_ascii=False),
            "message": message,
        }
        plan = self._fallback_plan()
        issues = []
        for attempt in range(2):
            try:
                response = await asyncio.wait_for(
                    self.chain.ainvoke(prompt_values),
                    timeout=get_settings().planner_timeout_seconds,
                )
                raw_content = str(getattr(response, "content", "")).strip()
                data = self._extract_json(raw_content)
                if not data:
                    raise ValueError("Missing plan")
                plan = AgentPlan.model_validate(data)
                self._coalesce_forecasts(plan)
                resolve_context(plan, previous_context or {}, self._normalize_mode(book_mode))
                return self._normalize_plan(plan, now, message, book_mode)
            except (asyncio.TimeoutError, json.JSONDecodeError, ValidationError, ValueError) as exc:
                detail = "; ".join(f"{error['loc']}: {error['msg']}" for error in exc.errors()) if isinstance(exc, ValidationError) else str(exc)
                issues.append({'type': type(exc).__name__, 'detail': detail[:800]})
                if attempt == 0:
                    prompt_values["message"] = message + "\n上次计划校验失败：" + detail[:800] + "\n请重新生成符合 JSON schema 的计划。用户的情景金额与次数必须填入 scenario.changes，预算和储蓄条件也要填写，不能只写在 finalInstruction。"

        plan = self._fallback_plan()
        plan._planning_issues = issues
        return plan

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
        self, plan: AgentPlan, now: datetime, message: str, book_mode: str = "个人版"
    ) -> AgentPlan:
        # normalize 的目的，是把“不稳定的 LLM 输出”整理成“稳定的程序输入”。
        self._coalesce_forecasts(plan)
        if plan.analysisType == "financial_analysis" and not plan.analyses:
            plan.analyses = [AnalysisRequest(tool=name) for name in ("cashflow", "trends", "changes", "anomalies", "recurring")]
        if plan.analyses:
            plan.intent = "analyze_orders"
            plan.needsTools = True
            if plan.analysisWindow and plan.analysisWindow.startDate > now.date():
                raise ValueError('analysisWindow 是已发生账单的历史范围，不能填未来购买或预测月份；历史范围未指定时填 null，未来月份填 targetMonth。')
            quantified_change = re.search(r"(?:增加|减少|新增|少花|多花|节省|多赚|少赚)[^\d，。；,\n]{0,6}\d+(?:\.\d+)?\s*(?:元|块|节|次)", message)
            if quantified_change and any(task.tool == "scenario" and not task.changes for task in plan.analyses):
                raise ValueError("scenario.changes 未提取用户明确给出的变化金额和次数")
            for task in plan.analyses:
                if task.tool == "budget" and task.budgetAmount is None and re.search(r"预算\D{0,6}\d", message):
                    raise ValueError("budget.budgetAmount 未提取用户明确给出的预算")
                if task.tool == "savings":
                    if task.startingBalance is None and re.search(r"(?:已有(?:存款|余额)?|(?:当前|现有|现在)的?(?:存款|余额)|存款|余额)\s*[为是有：:]?\s*\d", message):
                        raise ValueError("savings.startingBalance 未提取用户明确给出的当前余额")
                    if task.targetAmount is None and re.search(r"(?:存到|达到|目标)\D{0,6}\d", message):
                        raise ValueError("savings.targetAmount 未提取用户明确给出的目标金额")
                    if task.months is None and re.search(r"\d+\s*个月", message):
                        raise ValueError("savings.months 未提取用户明确给出的期限")
            relative = set(re.findall(r"下下(?:个)?月|下(?:个)?月|本月|这个月|上(?:个)?月", message))
            explicit_month = re.search(r"\d{1,4}[-年/]\d{1,2}|\d{1,2}\s*月", message)
            if len(relative) == 1 and not explicit_month:
                token = next(iter(relative))
                if token.startswith("下"):
                    target = shift_month(now.date(), 2 if token.startswith("下下") else 1).strftime("%Y-%m")
                    for task in plan.analyses:
                        if task.tool in {"forecast", "scenario"}:
                            task.targetMonth = target
            self._prepare_analysis_queries(plan, now, book_mode)
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

    def _coalesce_forecasts(self, plan):
        groups = {}
        for task in plan.analyses:
            if task.tool == 'forecast' and task.targetMonth:
                groups.setdefault(tuple(sorted(task.queryIds)), []).append(task)
        for query_ids, tasks in groups.items():
            targets = sorted({task.targetMonth for task in tasks})
            if len(targets) < 2:
                continue
            start = datetime.strptime(targets[0], '%Y-%m').date()
            contiguous = [shift_month(start, i).strftime('%Y-%m') for i in range(len(targets))]
            metrics = [{task.metric for task in tasks if task.targetMonth == target} for target in targets]
            if targets == contiguous and all(value == metrics[0] for value in metrics):
                plan.analyses = [task for task in plan.analyses if task not in tasks]
                plan.analyses.append(AnalysisRequest(tool='projection', queryIds=list(query_ids),
                                                     targetMonth=targets[0], months=len(targets)))

    def _prepare_analysis_queries(self, plan: AgentPlan, now: datetime, book_mode: str) -> None:
        forecasting = [task for task in plan.analyses if task.tool in {"forecast", "scenario", "savings", "projection", "purchase", "runway", "optimization"}]
        for task in forecasting:
            if task.targetMonth is None:
                task.targetMonth = shift_month(now.date(), 1).strftime("%Y-%m")
        # Expand date windows in code instead of asking a model to enumerate months.
        sources = {}
        source_scopes = {}
        for index, call in enumerate(plan.toolCalls, start=1):
            if call.toolName in PLANNER_TOOL_BY_NAME:
                key = (call.toolName, self._normalize_cost_type(call.args.cost_type), call.args.remark or "")
                identifier = call.id or f"query_{index}"
                if identifier in source_scopes and source_scopes[identifier] != key:
                    raise ValueError("An analysis references an ambiguous query id")
                source_scopes[identifier] = key
                sources.setdefault(key, []).append(identifier)
        if not sources:
            tool = "search_family_orders" if self._normalize_mode(book_mode) == "家庭版" else "search_personal_orders"
            sources[(tool, "不限", "")] = ["ledger"]
        known_ids = {identifier for identifiers in sources.values() for identifier in identifiers}
        if any(set(task.queryIds) - known_ids for task in plan.analyses):
            raise ValueError("An analysis references an unknown query id")
        cutoff = now.date().replace(day=1)
        for task in forecasting:
            target = datetime.strptime(task.targetMonth, "%Y-%m").date()
            cutoff = min(cutoff, target)
        periods = [shift_month(cutoff, offset) for offset in range(-6, 1)]
        if plan.analysisWindow:
            start = plan.analysisWindow.startDate.replace(day=1)
            end = plan.analysisWindow.endDate.replace(day=1)
            length = (end.year - start.year) * 12 + end.month - start.month + 1
            requested_periods = [shift_month(start, offset) for offset in range(length)]
            periods = sorted(set(periods + requested_periods)) if forecasting else requested_periods
        for task in plan.analyses:
            if task.tool == "budget":
                target = datetime.strptime(task.targetMonth, "%Y-%m").date() if task.targetMonth else now.date().replace(day=1)
                periods = sorted(set(periods + [target]))
        plan.toolCalls = [
            ToolCall(id=f"analysis_{index}_{period:%Y%m}", toolName=tool,
                     sourceIds=sources[(tool, category, remark)],
                     args=SearchPersonalOrdersArgs(year=period.year, month=period.month, cost_type=category, remark=remark),
                     reason="取得历史证据用于分析，区分完整月份和未完月份")
            for index, (tool, category, remark) in enumerate(sources, start=1)
            for period in periods
        ]

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
            tool_call.reason = format_tool_call(tool_call)
        return tool_call

    def _dedupe_tool_calls(self, tool_calls: list[ToolCall]) -> list[ToolCall]:
        # 对比类问题很容易让模型生成重复查询，这里根据参数去重。
        unique_calls: list[ToolCall] = []
        seen: dict[tuple, ToolCall] = {}
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
                existing = seen[key]
                existing.sourceIds = list(dict.fromkeys(
                    (existing.sourceIds or [existing.id]) + (tool_call.sourceIds or [tool_call.id])
                ))
                continue
            seen[key] = tool_call
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
        summaries = [format_tool_call(tool_call) for tool_call in plan.toolCalls]
        if len(summaries) > 1:
            return "、".join(summaries) + "后综合分析"
        return summaries[0] if summaries else "无需查询账单"

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
