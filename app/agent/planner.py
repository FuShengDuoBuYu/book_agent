import asyncio
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from langchain_core.prompts import ChatPromptTemplate
from langsmith import traceable
from pydantic import ValidationError

from app.core.config import get_settings
from app.core.llm import get_planner_model
from app.agent.analysis_context import resolve_context
from app.agent.plan_parsing import repair_descriptive_arrays
from app.agent.prompts import PLANNER_HUMAN_PROMPT, PLANNER_SYSTEM_PROMPT
from app.schemas.plan import (
    AgentPlan,
    AnalysisRequest,
    AnalysisWindow,
    ScenarioChange,
    SearchPersonalOrdersArgs,
    ToolCall,
    render_analysis_types_for_prompt,
    render_intents_for_prompt,
)
from app.tools.registry import PLANNER_TOOL_BY_NAME, format_tool_call, render_planner_tools_for_prompt
from app.tools.order_search import normalize_cost_type
from app.tools.financial_analysis import ANALYSIS_TOOLS, shift_month

HISTORY_BASED_TOOLS = {'forecast', 'scenario', 'savings', 'projection', 'purchase', 'runway', 'optimization'}


# A complete calendar month ends before the current local month begins.
_COMPLETE_MONTHS = re.compile(r'(?:最近|上)\s*([0-9]+|[一二两三四五六七八九十]+)\s*个?\s*完整(?:自然)?月份?')
_CHINESE_DIGITS = {char: number for number, char in enumerate('零一二三四五六七八九')} | {'两': 2}


def explicit_goal_amount(raw):
    """Parse bounded literal goal amounts; leave colloquial omitted units unknown."""
    if re.fullmatch(r'\d+(?:\.\d+)?万?', raw):
        return float(raw.rstrip('万')) * (10000 if raw.endswith('万') else 1)
    if re.search(r'[百千万亿][一二两三四五六七八九]$', raw):
        return None
    total = section = number = 0
    units = {'十': 10, '百': 100, '千': 1000, '万': 10000, '亿': 100000000}
    for char in raw:
        if char in _CHINESE_DIGITS:
            number = _CHINESE_DIGITS[char]
        elif char in units:
            unit = units[char]
            if unit < 10000:
                section += (number or 1) * unit
            else:
                total = (total + section + number) * unit
                section = 0
            number = 0
        else:
            return None
    return total + section + number


def explicit_complete_month_window(message, today):
    matches = list(_COMPLETE_MONTHS.finditer(message))
    if len(matches) != 1:
        return None
    match = matches[0]
    remaining = message[:match.start()] + message[match.end():]
    # Leave compound comparisons and explicitly anchored lookbacks to the planner.
    month_ref = r'(?:本月|这个月|当月|上个月|\d{4}[-年/.]\d{1,2}|(?:\d{1,2}|[一二三四五六七八九十]+)月)'
    if re.search(rf'截至|截止|(?:和|与|对比|相比|到|至)\s*{month_ref}|{month_ref}(?:至今)?\s*(?:和|与)', remaining):
        return None
    raw = match[1]
    if raw.isascii():
        count = int(raw)
    elif raw.count('十') == 1:
        tens, units = raw.split('十')
        if (tens and tens not in _CHINESE_DIGITS) or (units and units not in _CHINESE_DIGITS):
            return None
        count = _CHINESE_DIGITS.get(tens, 1) * 10 + _CHINESE_DIGITS.get(units, 0)
    else:
        count = _CHINESE_DIGITS.get(raw)
    if count is None:
        return None
    if not 1 <= count <= 24:
        raise ValueError('完整自然月范围应为 1 至 24 个月')
    current_month = today.replace(day=1)
    return AnalysisWindow(startDate=shift_month(current_month, -count), endDate=current_month - timedelta(days=1))


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
                metadata = getattr(response, 'response_metadata', {})
                issues.append({'type': 'model_call', 'phase': 'planning',
                               'promptTokens': metadata.get('prompt_eval_count'),
                               'outputTokens': metadata.get('eval_count'), 'doneReason': metadata.get('done_reason')})
                data = self._extract_json(raw_content)
                issues.extend(self._parse_repairs)
                if not data:
                    raise ValueError("Missing plan")
                plan = AgentPlan.model_validate(data)
                self._materialize_requirements(plan)
                requested_window = explicit_complete_month_window(message, now.date())
                if requested_window and requested_window != plan.analysisWindow:
                    plan.analysisWindow = requested_window
                    plan._planning_issues.append({'type': 'normalized_complete_month_window',
                        'window': requested_window.model_dump(mode='json')})
                self._preserve_referent_window(plan, previous_context or {}, message, now)
                context = self._inherit_conversation(plan, previous_context or {})
                self._normalize_explicit_conditions(plan, message)
                self._coalesce_forecasts(plan)
                resolve_context(plan, context, self._normalize_mode(book_mode))
                if requested_window and plan.intent == 'analyze_orders' and not plan.analyses and plan.toolCalls:
                    self._prepare_analysis_queries(plan, now, book_mode)
                plan._planning_issues.extend(issues)
                return self._normalize_plan(plan, now, message, book_mode)
            except (asyncio.TimeoutError, json.JSONDecodeError, ValidationError, ValueError) as exc:
                detail = "; ".join(f"{error['loc']}: {error['msg']}" for error in exc.errors()) if isinstance(exc, ValidationError) else str(exc)
                issues.append({'type': type(exc).__name__, 'detail': detail[:800]})
                if isinstance(exc, json.JSONDecodeError):
                    issues[-1]['outputExcerpt'] = raw_content[max(0, exc.pos - 60):exc.pos + 140]
                if attempt == 0:
                    prompt_values["message"] = message + "\n上次计划校验失败：" + detail[:800] + "\n仅修正这些结构或参数错误，保留当前用户目标、已选工具与已提供条件。不要为了修复结构而新增情景、金额、变化或无关字段。"

        plan = self._fallback_plan()
        plan._planning_issues = issues
        return plan

    def _extract_json(self, content: str) -> dict:
        self._parse_repairs = []
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

        content = content[start:end + 1]
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            repaired, self._parse_repairs = repair_descriptive_arrays(content)
            return json.loads(repaired)

    def _normalize_explicit_conditions(self, plan, message):
        """Recover narrow, unambiguous user inputs before resolving context.

        Multiple alternatives and negated statements remain the planner's job;
        this must never impose one scenario's numbers on another scope.
        """
        scopes = {(call.toolName, call.args.cost_type, call.args.remark) for call in plan.toolCalls}
        if len(scopes) > 1 or ('个人' in message and '家庭' in message):
            return
        clauses = re.split(r'[，。；,;\n]', message)
        positive = [clause for clause in clauses if not re.search(r'不是|并非|不会|并未|并不|不用|无需|不需要|不保留|不留', clause)]
        optimizations = [task for task in plan.analyses if task.tool == 'optimization']
        goal_pattern = r'(?:每月|每个月)\s*(?:额外)?\s*(?:多留(?:下)?|多存(?:下)?|多攒(?:下)?|多结余|多节省)\s*(\d+(?:\.\d+)?万?|[零一二两三四五六七八九十百千万亿]+)'
        goals = {explicit_goal_amount(raw) for clause in positive for raw in re.findall(goal_pattern, clause)} - {None}
        if len(optimizations) == 1 and len(goals) == 1:
            amount = goals.pop()
            if 0 <= amount <= 1e12:
                task = optimizations[0]
                task.monthlyImprovement = amount
                # A desired improvement is not a realized/assumed spending cut.
                # Keep separately stated source-specific or other scenario changes.
                if not re.search(r'假设|已经|已减少|少花|支出减少', message):
                    task.changes = [change for change in task.changes if not (
                        change.metric == 'expense' and not change.source and change.amount is not None
                        and -change.amount * change.count == amount)]
                plan._planning_issues.append({'type': 'normalized_explicit_condition', 'field': 'monthlyImprovement', 'value': amount})
        purchases = [task for task in plan.analyses if task.tool == 'purchase']
        amounts = set()
        patterns = (r'(?:保留|预留|留出|留)\s*(\d+(?:\.\d+)?)\s*(万)?\s*(?:元|块)?\s*(?:的|作为)?(?:应急|备用|储备)',
                    r'(?:应急金|应急钱|备用金|储备金|储备底线)\s*(?:设为|至少|为|是|要)?\s*(\d+(?:\.\d+)?)\s*(万)?')
        for clause in positive:
            for pattern in patterns:
                amounts.update(float(amount) * (10000 if unit else 1) for amount, unit in re.findall(pattern, clause))
        if len(purchases) == 1 and len(amounts) == 1:
            reserve = amounts.pop()
            if not 0 <= reserve <= 1e12:
                raise ValueError('reserveAmount 超出允许范围')
            task = purchases[0]
            if task.reserveAmount != reserve:
                task.reserveAmount = reserve
                # A wrongly classified reserve must not become a future goal.
                if task.targetAmount == reserve and not any(t.tool == 'savings' for t in plan.analyses):
                    task.targetAmount = None
                plan._planning_issues.append({'type': 'normalized_explicit_condition', 'field': 'reserveAmount', 'value': reserve})
        scenarios = [task for task in plan.analyses if task.tool in {'runway', 'projection', 'scenario', 'savings'}]
        stopped = any(re.search(r'(?:(?:所有|全部)(?:的)?收入(?:都|完全|全部)?|收入(?:全部|全都|都))(?:停了|停止|中断|归零|没有了)', clause) for clause in positive)
        if stopped and len(scenarios) == 1 and not re.search(r'比较|对比|两种|分别', message):
            task = scenarios[0]
            if not any(change.metric == 'income' and not change.source and change.percent == -100 for change in task.changes):
                task.changes = [change for change in task.changes if change.metric != 'income'] + [
                    ScenarioChange(metric='income', percent=-100, source='', label='用户明确假设全部收入中断')]
                plan._planning_issues.append({'type': 'normalized_explicit_condition', 'field': 'changes', 'detail': '用户明确假设全部收入中断，按全部收入减少100%计算'})

    def _normalize_plan(
        self, plan: AgentPlan, now: datetime, message: str, book_mode: str = "个人版"
    ) -> AgentPlan:
        # normalize 的目的，是把“不稳定的 LLM 输出”整理成“稳定的程序输入”。
        self._coalesce_forecasts(plan)
        self._materialize_requirements(plan)
        if plan.analysisType == "financial_analysis" and not plan.analyses:
            plan.analyses = [AnalysisRequest(tool=name) for name in ("cashflow", "trends", "changes", "anomalies", "recurring")]
        if plan.analyses:
            plan.intent = "analyze_orders"
            plan.needsTools = True
            explicit_history = self._has_history_reference(message, now)
            if (plan.analysisWindow and plan.analysisWindow.startDate >= now.date().replace(day=1)
                    and any(task.tool in HISTORY_BASED_TOOLS for task in plan.analyses) and not explicit_history):
                plan._planning_issues.append({'type': 'normalized_implicit_history_window',
                    'detail': '用户未限定历史月份；当前余额或未来目标不将历史样本限制为本月。'})
                plan.analysisWindow = None
            if plan.analysisWindow and plan.analysisWindow.startDate > now.date():
                if not any(task.tool in HISTORY_BASED_TOOLS for task in plan.analyses):
                    raise ValueError('analysisWindow 不能用未来月份查询已发生的历史收支；未来估计需要预测工具。')
                # A future decision date is not a sample of recorded transactions.
                # Let the existing history expansion supply the decision baseline.
                plan._planning_issues.append({'type': 'normalized_future_window',
                    'window': plan.analysisWindow.model_dump(mode='json'),
                    'detail': '未来决策月份误放入历史窗口，改用默认历史基准；预测目标保持在 targetMonth。'})
                plan.analysisWindow = None
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
            if not plan._analysis_context.get('ambiguous'):
                future = {task.targetMonth for task in plan.analyses if task.tool in HISTORY_BASED_TOOLS}
                if len(future) == 1:
                    plan._analysis_context['targetMonth'] = next(iter(future))
                for task in plan.analyses:
                    if task.tool == 'budget':
                        plan._analysis_context['budgetTargetMonth'] = task.targetMonth
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

    @staticmethod
    def _has_history_reference(message, now):
        def historical_date(match):
            year, month = map(int, match.groups())
            return match[0] if (year, month) <= (now.year, now.month) else ''
        historical_message = re.sub(r'(\d{4})\s*[-年/]\s*(\d{1,2})\s*月?', historical_date, message)
        return bool(re.search(r'本月|这个月|当月|今天|昨天|前天|本周|这周|上周|上(?:上)?(?:个)?月|去年|前年|今年|本季度|上季度|最近|过去|\d{1,4}[-年/]\d{1,2}|(?:\d{1,2}|[一二三四五六七八九十]+)月', historical_message))

    def _preserve_referent_window(self, plan, previous, message, now):
        if not (plan.followUpOfPreviousQuestion and not plan.resetAnalysisContext and previous.get('conversation')):
            return
        if plan.toolCalls:
            scope = sorted({(call.toolName, self._normalize_cost_type(call.args.cost_type), call.args.remark)
                            for call in plan.toolCalls})
            if [list(row) for row in scope] != previous.get('scope'):
                return
        if self._has_history_reference(message, now):
            return
        window = previous.get('activeHistoryWindow', previous['conversation'].get('analysisWindow'))
        current = plan.analysisWindow.model_dump(mode='json') if plan.analysisWindow else None
        if current != window:
            plan.analysisWindow = AnalysisWindow.model_validate(window) if window else None
            plan._planning_issues.append({'type': 'preserved_referent_window',
                'detail': '追问未修改历史时间范围，保留被追问分析的样本窗口。'})

    def _materialize_requirements(self, plan):
        requirements = set(plan.understanding.evidenceNeeds)
        tools = set()
        future = any(task.tool in HISTORY_BASED_TOOLS for task in plan.analyses)
        if ('trends' in requirements or (plan.output.chart != 'none' and not any(task.tool == 'aggregate' for task in plan.analyses))) and not future:
            tools.add('trends')
        if requirements & {'sources', 'members', 'remarks'}:
            tools.add('sources')
        if 'forecast' in requirements and not future:
            tools.add('forecast')
        if 'changes' in requirements:
            tools.add('changes')
        plan.analyses.extend(AnalysisRequest(tool=tool, metric=plan.output.metric if plan.output.metric != 'all' else 'income')
                             for tool in sorted(tools) if not any(task.tool == tool for task in plan.analyses))

    def _inherit_conversation(self, plan, previous):
        """Only inherit a referent's range and grouping within its ledger/filter scope."""
        conversation = previous.get('conversation', {})
        if not (plan.followUpOfPreviousQuestion and not plan.resetAnalysisContext and conversation):
            return previous
        if not plan.toolCalls:
            templates = conversation.get('queries') or []
            if not templates and len(previous.get('scope', [])) == 1:
                tool, category, remark = previous['scope'][0]
                identifiers = {key for row in conversation.get('analyses', []) for key in row.get('queryIds', [])} or {'ledger'}
                templates = [{'id': key, 'toolName': tool, 'args': {'cost_type': category, 'remark': remark}}
                             for key in sorted(identifiers)]
            restored = [ToolCall.model_validate(row) for row in templates]
            restored_scope = sorted({(call.toolName, call.args.cost_type, call.args.remark) for call in restored})
            if [list(row) for row in restored_scope] != previous.get('scope'):
                return {}
            plan.toolCalls = restored
        scopes = sorted({(call.toolName, call.args.cost_type, call.args.remark) for call in plan.toolCalls})
        if [list(row) for row in scopes] != previous.get('scope'):
            return {}
        if not plan.analyses and plan.intent == 'analyze_orders':
            plan.analyses = [AnalysisRequest.model_validate(row) for row in conversation.get('analyses', [])]
        active = previous.get('activeAnalyses') or [row for row in conversation.get('analyses', []) if row.get('tool') != 'sources']
        previous_window = previous.get('activeHistoryWindow', conversation.get('analysisWindow'))
        same_window = not plan.analysisWindow or plan.analysisWindow.model_dump(mode='json') == previous_window
        if not plan.sourceGroups:
            from app.schemas.plan import SourceGroup
            plan.sourceGroups = [SourceGroup.model_validate(row) for row in conversation.get('sourceGroups', [])]
        sources_only = bool(plan.analyses) and all(task.tool == 'sources' for task in plan.analyses)
        if sources_only and not same_window:
            # The explicit historical range establishes a new referent for future turns.
            # Preserve the restored query scope, but drop the superseded decision conditions.
            return {}
        if plan.intent == 'analyze_orders' and sources_only and active:
            # Inspecting records is a subtask of the referenced analysis. Keep the
            # active calculation available when a later question asks how it worked.
            restored_tasks = [AnalysisRequest.model_validate(row) for row in active]
            known_ids = {call.id for call in plan.toolCalls}
            for task in restored_tasks:
                if set(task.queryIds) - known_ids and len(scopes) == 1:
                    task.queryIds = sorted(known_ids)
            plan.analyses.extend(restored_tasks)
        if not plan.analysisWindow and conversation.get('analysisWindow'):
            plan.analysisWindow = AnalysisWindow.model_validate(conversation['analysisWindow'])
        return previous

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
        forecasting = [task for task in plan.analyses if task.tool in HISTORY_BASED_TOOLS]
        for task in forecasting:
            if task.targetMonth is None:
                task.targetMonth = shift_month(now.date(), 1).strftime("%Y-%m")
        for task in plan.analyses:
            if task.tool == 'budget' and task.targetMonth is None:
                task.targetMonth = now.strftime('%Y-%m')
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
            if length > 24 and any(task.tool == 'aggregate' for task in plan.analyses):
                raise ValueError('汇总区间跨度不能超过 24 个自然月')
            requested_periods = [shift_month(start, offset) for offset in range(length)]
            periods = requested_periods
        for task in plan.analyses:
            if task.tool == "budget":
                target = datetime.strptime(task.targetMonth, "%Y-%m").date() if task.targetMonth else now.date().replace(day=1)
                periods = sorted(set(periods + [target]))
        calls = []
        for index, ((tool, category, remark), identifiers) in enumerate(sources.items(), 1):
            query_months = periods
            if not plan.analysisWindow:
                bound = [task for task in plan.analyses if not task.queryIds or set(identifiers).intersection(task.queryIds)]
                explicit = [task for task in bound if task.tool == 'aggregate' and task.aggregate and task.aggregate.periods]
                if explicit:
                    ranges = [period for task in explicit for period in task.aggregate.periods]
                    start = min(period.startDate for period in ranges).replace(day=1)
                    end = max(period.endDate for period in ranges).replace(day=1)
                    length = (end.year - start.year) * 12 + end.month - start.month + 1
                    if length > 24:
                        raise ValueError('汇总区间跨度不能超过 24 个自然月')
                    aggregate_months = [shift_month(start, offset) for offset in range(length)]
                    query_months = aggregate_months if len(explicit) == len(bound) else sorted(set(periods + aggregate_months))
            calls.extend(ToolCall(id=f"analysis_{index}_{period:%Y%m}", toolName=tool, sourceIds=identifiers,
                                  args=SearchPersonalOrdersArgs(year=period.year, month=period.month, cost_type=category, remark=remark),
                                  reason="取得历史证据用于分析，区分完整月份和未完月份") for period in query_months)
        plan.toolCalls = calls

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
        return normalize_cost_type(value)

    def _should_use_month_without_year(self, message: str) -> bool:
        text = message.strip()
        return any(
            keyword in text
            for keyword in ["不限年份", "所有年份", "全部年份", "历年", "每一年"]
        )
