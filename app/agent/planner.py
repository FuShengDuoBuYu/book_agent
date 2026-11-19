import asyncio
import json
import re
from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from langchain_core.prompts import ChatPromptTemplate
from langsmith import traceable
from pydantic import ValidationError

from app.core.config import get_settings
from app.core.llm import get_planner_model
from app.agent.analysis_context import remove_withdrawn_changes, resolve_context, source_direction_of
from app.agent.intent_resolution import IntentResolver
from app.agent.conversation_context import compact_user_context
from app.agent.plan_parsing import repair_descriptive_arrays
from app.agent.report_context import build_report_context
from app.agent.plan_review import NUMBER, PlanReviewer, _duration_evidence, calendar_context, explicit_month_delta, explicit_projection_start, named_percent_changes, sanitize_user_conditions
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
from app.tools.aggregate_analysis import AggregatePeriod

HISTORY_BASED_TOOLS = {'forecast', 'scenario', 'savings', 'projection', 'purchase', 'runway', 'optimization'}


_LEDGER_SCOPES = re.compile(
    r'(?P<family>家庭(?:版|账本|账单|账户|成员)|(?:家庭|全家)(?=的?(?:收入|收支|开销|支出|账))'
    r'|(?:我们家|我家)的?(?:账本|账单|账户)|我们家(?!附近|旁边|门口|楼下)|我家(?=的?(?:收入|收支|开销|支出|账|成员)))'
    r'|(?P<personal>个人(?:版|账本|账单|账户)|(?:我个人|我自己)|个人(?=的?(?:收入|收支|开销|支出|账)))')
_LEDGER_PAIR = re.compile(
    r'(?:我(?:个人)?|个人(?:版|账本|账单)?)\s*(?:和|与|跟|及|、|对比)\s*(?:我们家|全家|家庭(?=$|[的版账各分，。？\s]|收入|支出|开销|收支))'
    r'|(?:我们家|全家|家庭(?:版|账本|账单)?)\s*(?:和|与|跟|及|、|对比)\s*(?:我个人|个人)'
    r'|(?:两个|两[个份套]?|双)账本')


def _requested_ledger_modes(message, default_mode):
    """Only the current request can select a different ledger from the UI."""
    # Quoted values of record fields describe transactions, including when the
    # literal remark or category happens to contain a ledger name.
    text = re.sub(r'(?:备注|分类|类别|来源)(?:里|中|名称|名)?(?:写着|写的是|包含|含|叫|为|是)?\s*[“「『"\']'
                  r'[^”」』"\']*[”」』"\']', '', message)
    clauses = []
    for clause in re.split(r'[，,；;。！？!?\n]|(?:但)?(?:这次|这回|本次|现在)(?:改为|改用|换到|只看)?', text):
        if re.search(r'(?:上次|上一轮|刚才|之前|先前)(?:我[们]?|你)?(?:看|查|用|选|说|提|算|讲|是|的范围)', clause):
            continue
        clauses.append(clause)
    current = '，'.join(clauses)
    excluded, selected = set(), []
    previous = None
    coordinated = False
    for m in _LEDGER_SCOPES.finditer(current):
        mode = '家庭版' if m.lastgroup == 'family' else '个人版'
        if re.search(r'(?:不要|不用|不再|不|别|排除)(?:看|再看|查|再查|用|使用|包括|包含|是)?(?:我的|我们的)?\s*$', current[:m.start()]):
            excluded.add(mode)
            continue
        if previous and previous.lastgroup != m.lastgroup:
            between = current[previous.end():m.start()]
            coordinated |= bool(re.search(r'和|与|跟|以及|、|对比|对照|相比|，(?:再|也|另外)(?:查|看|核对)', between))
        selected.append((mode, m.group().endswith(('账本', '版', '账户', '账单'))))
        previous = m
    both = {'家庭版', '个人版'}
    if not excluded and (_LEDGER_PAIR.search(current) or coordinated):
        return both
    # An explicit ledger can be filtered to one member ("家庭账本里我个人的收入").
    explicit = [mode for mode, ledger in selected if ledger]
    if explicit or selected:
        return {explicit[-1] if explicit else selected[-1][0]}
    return both - excluded if len(excluded) == 1 else {default_mode}


# A complete calendar month ends before the current local month begins.
_COMPLETE_MONTHS = re.compile(r'(?:最近|上)\s*([0-9]+|[一二两三四五六七八九十]+)\s*个?\s*完整(?:自然)?月份?')
_CHINESE_DIGITS = {char: number for number, char in enumerate('零一二三四五六七八九')} | {'两': 2}


_MONTHLY_GOAL = r'(?:每月|每个月)\s*(?:还)?(?:想|希望|要)?(?:能)?\s*(?:额外)?\s*(?:多留(?:下)?|多存(?:下)?|多攒(?:下)?|多结余|多节省)\s*(\d+(?:\.\d+)?万?|[零一二两三四五六七八九十百千万亿]+)'


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


def _historical_half_year(message, today):
    if not re.search(r"(?:最近|近)\s*半年", message):
        return None
    # An explicit endpoint or a separate comparison supplies different boundaries.
    if re.search(r"截至|截止|同期|不含本月|不包括本月|去年|前年|以前的近半年", message):
        return None
    return AnalysisWindow(startDate=shift_month(today.replace(day=1), -6), endDate=today)


def _explicit_history_sample(message, today):
    """Separate a bounded lookback from the goal date or date of a purchase."""
    if re.search(r"(?:最近|过去|近|前)\s*(?:半|数|几|" + NUMBER +
                 r")\s*个?\s*(?:完整(?:自然)?)?(?:天|日|周|星期|月|季|年)|上(?:一|上)?个?(?:完整)?月|上周|上季度|去年|前年", message):
        return True
    stamp = re.compile(r"(?:(\d{4})\s*[-年/.]\s*(\d{1,2})\s*月?|"
                       r"(明年|今年)?\s*(\d{1,2}|[一二三四五六七八九十]+)月份?|"
                       r"(\d{4})年|(明年|今年|当年|本月|这个月|当月|本周|这周|本季度|今天|昨天|前天))")
    for clause in re.split(r"[，。；！？,;\n]|(?:并且?|然后|再)(?=预测|预计|估算|推演)", message):
        for match in stamp.finditer(clause):
            year, month, relative_year, named_month, bare_year, token = match.groups()
            if token == '明年' or relative_year == '明年':
                continue
            y = int(year or bare_year or today.year)
            m = int(month or (explicit_goal_amount(named_month) if named_month else today.month))
            if (y, m) > (today.year, today.month):
                continue
            before, after = clause[:match.start()], clause[match.end():]
            # A bare month after "预测" names the estimation target, even if its
            # next occurrence falls in the next calendar year.
            if re.search(r"(?:参考|基于|根据|仅用|只用|使用|样本取|截至|回顾|分析|对照|从|按)\s*$", before):
                if not re.match(r"(?:份)?(?:开始|起算|起)(?:预测|推演)", after):
                    return True
            if re.match(r"(?:的)?(?:已记|记录|数据|流水|账本|账单|历史|样本|至今|以来)", after):
                return True
            if (not re.search(r"预测|预计|估算|预估|推演|目标|改到|计划|购买|想买|要买|打算|能买|下单|余额|可动用", clause)
                    and not re.search(r"未来|明年|下个?月", before)):
                return True
    return False


def _future_month_count(message):
    match = re.findall(rf"(?:未来|接下来|今后|往后)\s*({NUMBER})\s*个月(?!\s*后)", message)
    match.extend(re.findall(rf"(?:按|改为|改成)\s*({NUMBER})\s*个月\s*(?=(?:来|重新|再|重)?(?:推演|预测|测算|估算|计算))", message))
    if len(set(match)) == 1:
        value = explicit_goal_amount(match[0])
        if value is not None and value == int(value) and 1 < value <= 600:
            return int(value)
    return None


def _relative_month_comparison(message, today):
    """Resolve two explicitly compared whole calendar months from the local date."""
    token = r"(?:上上?个?月|本月|这个月)"
    match = re.search(rf"({token})\s*(?:和|与|对比|相比|跟|、)\s*({token})", message)
    if not match or len(re.findall(token, message)) != 2:
        return []
    rest = message[:match.start()] + message[match.end():]
    if re.search(r"同期|上旬|下旬|中旬|第.{1,3}[天周]|月初|月底|截至|截止|\d+\s*[日号月]|[一二三四五六七八九十]+月", rest):
        return []
    periods = {}
    for label in match.groups():
        start = shift_month(today.replace(day=1), -label.count("上"))
        periods[start] = AggregatePeriod(label=label, startDate=start, endDate=min(today, shift_month(start, 1)-timedelta(days=1)))
    return [periods[start] for start in sorted(periods)] if len(periods) == 2 else []


class QueryPlanner:
    def __init__(self) -> None:
        # Planner 不直接回答用户，它只把自然语言问题翻译成结构化执行计划。
        self.prompt = ChatPromptTemplate.from_messages(
            [
                ("system", PLANNER_SYSTEM_PROMPT),
                ("human", PLANNER_HUMAN_PROMPT + "\n当前日期对应的日历月份：{calendar_context}"),
            ]
        ).partial(
            tools_text=render_planner_tools_for_prompt(),
            intent_types_text=render_intents_for_prompt(),
            analysis_types_text=render_analysis_types_for_prompt(),
            analysis_tools_text="\n".join(f"- {name}: {description}" for name, description in ANALYSIS_TOOLS.items()),
        )
        self.chain = self.prompt | get_planner_model()
        self.intent_resolver = IntentResolver()
        self.reviewer = PlanReviewer()

    @traceable(name="query_planner", run_type="chain")
    async def create_plan(
        self, message: str, history: str, book_mode: str = "个人版", previous_context: dict | None = None
    ) -> AgentPlan:
        # 这里的核心任务是：拿到 LLM 输出后，尽量把它收敛成一个可执行的 AgentPlan。
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        modes = _requested_ledger_modes(message, self._normalize_mode(book_mode))
        if len(modes) == 1:
            book_mode = next(iter(modes))
        previous_context = deepcopy(previous_context or {})
        if previous_context.get('scope'):
            previous_context['scope'] = [list(row) for row in sorted({
                (tool, self._normalize_cost_type(category), remark or '')
                for tool, category, remark in previous_context['scope']})]
            prior_modes = {PLANNER_TOOL_BY_NAME.get(row[0], (None,))[0] for row in previous_context['scope']}
            if prior_modes != modes:
                previous_context = {}
        prompt_values = {
            "current_date": now.strftime("%Y-%m-%d"),
            "calendar_context": json.dumps(calendar_context(now.date()), ensure_ascii=False),
            "book_mode": self._normalize_mode(book_mode),
            "history": history + '\n上轮已解析的用户条件（追问时沿用，独立新问题不沿用）：' + json.dumps(previous_context or {}, ensure_ascii=False),
            "message": message,
        }
        plan = self._fallback_plan()
        issues = []
        resolved_goal = None
        if getattr(self, 'intent_resolver', None):
            resolved_goal, audit = await self.intent_resolver.resolve(
                message, history, now.date(), self._normalize_mode(book_mode), previous_context)
            issues.append({'type': 'intent_resolution', **audit,
                           'understanding': resolved_goal.model_dump() if resolved_goal else None})
        for attempt in range(2):
            try:
                if getattr(self, 'prompt', None):
                    # Native JSON grammar is not chat text. Preserve the current
                    # goal and scoped conditions before fitting conversation text.
                    system = self.prompt.format_messages(**{**prompt_values, 'history': ''})[0].content
                    bounded, _, budget = build_report_context({
                        'question': prompt_values['message'], 'history': history,
                        'understanding': resolved_goal.model_dump() if resolved_goal else {},
                        'userConditions': compact_user_context(previous_context)}, [], system, {},
                        num_ctx=get_settings().ollama_num_ctx, num_predict=get_settings().planner_num_predict)
                    prompt_values['history'] = bounded['history'] + '\n已解析的有范围条件：' + json.dumps(
                        bounded['userConditions'], ensure_ascii=False, separators=(',', ':'))
                    if resolved_goal:
                        prompt_values['history'] += '\n本轮意图理解（逐项落实到执行参数）：' + json.dumps(
                            resolved_goal.model_dump(), ensure_ascii=False, separators=(',', ':'))
                    issues.append({'type': 'planning_context', **budget})
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
                if resolved_goal and not plan.understanding.objective:
                    plan.understanding = resolved_goal.model_copy(deep=True)
                if resolved_goal:
                    relation = resolved_goal.conversationRelation
                    if relation in {'follow_up', 'correction'}:
                        plan.followUpOfPreviousQuestion = True
                        plan.resetAnalysisContext = False
                    elif relation in {'independent', 'reset'}:
                        plan.followUpOfPreviousQuestion = False
                        plan.resetAnalysisContext = relation == 'reset'
                self._normalize_query_scopes(plan, modes)
                self._materialize_requirements(plan, message, previous_context, book_mode)
                self._sanitize_conditions(plan, message, book_mode, previous_context)
                if (plan.intent == 'analyze_orders' and (plan.analyses or plan.analysisType == 'financial_analysis')
                        and getattr(self, 'reviewer', None)):
                    decision, plan._semantic_review = await self.reviewer.review(
                        message, history, plan, now.date(), self._normalize_mode(book_mode), previous_context)
                    plan._semantic_review['adopted'] = bool(decision is not None and not decision.adequate)
                    if decision is not None and not decision.adequate:
                        plan.analyses = decision.analyses
                        plan.analysisWindow = decision.analysisWindow
                        plan.finalInstruction = decision.answerInstruction
                        self._materialize_requirements(plan, message, previous_context, book_mode)
                        self._sanitize_conditions(plan, message, book_mode, previous_context)
                requested_window = explicit_complete_month_window(message, now.date()) or _historical_half_year(message, now.date())
                if requested_window and requested_window != plan.analysisWindow:
                    plan.analysisWindow = requested_window
                    plan._planning_issues.append({'type': 'normalized_complete_month_window',
                        'window': requested_window.model_dump(mode='json')})
                relative_periods = _relative_month_comparison(message, now.date())
                if relative_periods:
                    self._align_calendar_descriptions(plan, message, relative_periods)
                    plan.analysisWindow = AnalysisWindow(startDate=relative_periods[0].startDate, endDate=relative_periods[-1].endDate)
                    for task in plan.analyses:
                        if task.aggregate:
                            task.aggregate.periods = relative_periods
                self._preserve_referent_window(plan, previous_context or {}, message, now)
                context = self._inherit_conversation(plan, previous_context or {})
                if len(modes) == 2 and plan.intent == 'analyze_orders':
                    queried = {PLANNER_TOOL_BY_NAME[call.toolName][0] for call in plan.toolCalls
                               if call.toolName in PLANNER_TOOL_BY_NAME}
                    if queried != modes:
                        raise ValueError('用户要求同时核对两个账本，请分别绑定个人和家庭查询。')
                self._normalize_explicit_conditions(plan, message)
                self._normalize_projection(plan, message, context, book_mode, now.date())
                self._sanitize_conditions(plan, message, book_mode, context)
                self._coalesce_forecasts(plan)
                resolve_context(plan, context, self._normalize_mode(book_mode))
                self._sanitize_conditions(plan, message, book_mode, context, prune_prior=False)
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

    @staticmethod
    def _align_calendar_descriptions(plan, message, periods):
        # Relative words in the actual question remain valid when an annotation
        # paired them with the wrong absolute month. Keep the requested deliverables.
        expected = {p.startDate.strftime("%Y-%m") for p in periods}
        canonical = "；".join(f"{p.label}为{p.startDate:%Y-%m}" for p in periods)
        def conflicts(text):
            concrete = re.findall(r"(\d{4})\s*[-年/.]\s*(\d{1,2})", text)
            if any(f"{int(y):04}-{int(m):02}" not in expected for y,m in concrete):
                return True
            months = {p.startDate.month for p in periods}
            if any(int(m) not in months for m in re.findall(r"(?<!\d)(\d{1,2})\s*月份?", text)):
                return True
            for p in periods:
                label = r"(?<!上)" + ("上" * p.label.count("上") + "个?月" if "上" in p.label else "本月|这个月")
                paired = re.search("(?:" + label + ")" + r"\s*[（(：:=为是指 ]+\s*(\d{4})[-年/.](\d{1,2})", text)
                if paired and (int(paired[1]), int(paired[2])) != (p.startDate.year, p.startDate.month):
                    return True
            return False
        u = plan.understanding
        if conflicts(u.objective):
            u.objective = message[:600]
        if any(conflicts(answer) for answer in u.requestedAnswers):
            u.requestedAnswers = ([a for a in u.requestedAnswers if not conflicts(a)][:7] + [message[:600]])
        u.referents = ([r for r in u.referents if not conflicts(r) and r != canonical][:7] + [canonical])
        if conflicts(plan.finalInstruction):
            plan.finalInstruction = f"按用户问题逐项回答。历史核算月份为{canonical}。"
        plan._planning_issues.append({"type": "resolved_calendar_months", "periods": [p.model_dump(mode="json") for p in periods]})

    def _normalize_projection(self, plan, message, context, book_mode, today):
        if plan.intent != 'analyze_orders' or not plan.analyses:
            return
        scopes = {(c.toolName, c.args.cost_type, c.args.remark) for c in plan.toolCalls}
        if not scopes:
            default = 'search_family_orders' if self._normalize_mode(book_mode) == '家庭版' else 'search_personal_orders'
            scopes = {(default, '不限', '')}
        if len(scopes) > 1 or ('个人' in message and '家庭' in message):
            return
        prior = context if (plan.followUpOfPreviousQuestion and not plan.resetAnalysisContext
                            and not context.get('ambiguous') and len(context.get('scope', [])) == 1
                            and list(next(iter(scopes))) == context['scope'][0]) else {}
        active = [row for row in prior.get('activeAnalyses', []) if row.get('tool') == 'projection']
        projections = [t for t in plan.analyses if t.tool == 'projection']
        scenarios = [t for t in plan.analyses if t.tool == 'scenario']
        intent = ' '.join([plan.understanding.objective, *plan.understanding.requestedAnswers])
        conditions = named_percent_changes(message)
        horizon = _future_month_count(message)
        delta = explicit_month_delta(message)
        other_goals = any(t.tool in {'purchase', 'runway', 'savings', 'budget', 'optimization'} for t in plan.analyses)
        cumulative = bool(re.search(r'累计.*?结余|多月推演|累计.*?存', intent))
        withdrawn = any(issue['type'] == 'withdrawn_source_assumption' for issue in plan._planning_issues)
        protected_adjustment = any(t.tool == 'optimization' and t.protectedCategories for t in plan.analyses)
        amend = (len(active) == 1 and not re.search(r'只(?:看|算)|仅(?:看|算)|下下?个?月(?:的|会|能)', message)
                 and ((not other_goals and (conditions or withdrawn)) or delta is not None or cumulative or protected_adjustment))
        outlook = bool(scenarios or any(t.tool == 'forecast' for t in plan.analyses)
                       or re.search(r'收支|结余|能存|收入预测|支出预测', message))
        if not projections and ((horizon and outlook and not other_goals) or amend):
            if len(scenarios) == 1:
                task = scenarios[0]
                task.tool = 'projection'
            elif not scenarios:
                task = AnalysisRequest(tool='projection')
                plan.analyses.append(task)
            else:
                raise ValueError('多个情景需要分别绑定预测期限')
            projections = [task]
        if (delta and prior.get('months') and len(projections) > 1 and not conditions
                and not re.search(r'如果|假设|不同情景|两种情景|分别按', message)):
            # One amended deadline has one active scenario. previousMonths supplies
            # the old horizon for the reporter's before/after calculation.
            revised = prior['months'] + delta
            projection = next((t for t in projections if t.months == revised), projections[0])
            plan.analyses = [t for t in plan.analyses if t.tool != 'projection' or t is projection]
            projections = [projection]
        if len(projections) == 1:
            projection = projections[0]
            start = explicit_projection_start(message, today)
            if horizon:
                projection.months = horizon
            elif amend:
                projection.months = prior.get('months') or projection.months
            if start:
                projection.targetMonth = start
            elif amend:
                projection.targetMonth = prior.get('targetMonth') or projection.targetMonth
            if delta is not None and prior.get('months'):
                projection.monthsDelta = delta
        if delta and prior.get('months') and not conditions:
            explicit, _ = self._decision_goals(message, plan, prior, book_mode)
            if not explicit - {'savings', 'projection'}:
                active_goals = {row.get('tool') for row in prior.get('activeAnalyses', [])}
                primary = None
                if 'savings' in active_goals and prior.get('targetAmount') is not None:
                    primary = 'savings'
                    savings = [t for t in plan.analyses if t.tool == 'savings']
                    if not savings:
                        savings = [AnalysisRequest(tool='savings')]
                        plan.analyses.extend(savings)
                    if len(savings) == 1:
                        savings[0].monthsDelta = delta
                elif len(active) == 1 and len(projections) == 1 and prior.get('targetAmount') is None:
                    primary = 'projection'
                if primary:
                    # The amended goal owns the comparison. Supporting calculations
                    # still execute with the same horizon and protected categories.
                    for task in plan.analyses:
                        if task.tool in {'projection', 'optimization'} and task.tool != primary and task.tool not in explicit:
                            task._supplemental = True
        adjustable = [t for t in plan.analyses if t.tool in {'projection', 'scenario', 'runway', 'savings', 'optimization'}]
        if not adjustable or not conditions:
            return
        # An existing source-specific model input has already passed the provenance
        # check. Materialize omissions only for a single literal source and scenario.
        if len(conditions) != 1 or len({(t.tool, tuple(t.queryIds)) for t in adjustable}) != len(adjustable):
            if not all(any(c.source in {source, source+'收入', source+'支出'} and c.percent == rate
                           and role in (None, c.metric)
                           for t in adjustable for c in t.changes) for source, role, rate in conditions):
                raise ValueError('每个明确的来源变化必须绑定到对应情景，不能作为所有收入或支出的变化')
            return
        literal, metric, percent = conditions[0]
        name = literal
        if not metric:
            observation = source_direction_of(literal, prior)
            if observation:
                name, metric = observation
            elif re.search(r'不是.{1,32}?(?:，|,|而)是', message):
                # A correction of only the source carries the role of the user's
                # single retracted input (e.g. the income in the original cut).
                roles = {i['metric'] for i in plan._planning_issues if i.get('type') == 'withdrawn_source_assumption'}
                if len(roles) == 1:
                    metric = roles.pop()
            if not metric:
                raise ValueError(f'请明确「{literal}」的变化属于收入还是支出，并将该来源和比例绑定到情景计算')
        change = ScenarioChange(metric=metric, source=name, percent=percent)
        for task in adjustable:
            task.changes = [c for c in task.changes if c.source not in {name, literal, literal+'收入', literal+'支出'}
                            or c.percent != percent and c.metric != metric] + [change]
        plan._planning_issues.append({'type': 'normalized_explicit_condition', 'field': 'changes', 'source': name})

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
        self._ground_spending_request(plan, message)
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
        self._materialize_requirements(plan, message, plan._analysis_context, book_mode)
        if plan.analysisType == "financial_analysis" and not plan.analyses:
            plan.analyses = [AnalysisRequest(tool=name) for name in ("cashflow", "trends", "changes", "anomalies", "recurring")]
        if plan.analyses:
            plan.intent = "analyze_orders"
            plan.needsTools = True
            future = any(t.tool in HISTORY_BASED_TOOLS for t in plan.analyses)
            inherited = (plan.followUpOfPreviousQuestion and not plan.resetAnalysisContext
                         and bool(plan._analysis_context.get('conversation')))
            if plan.analysisWindow and future and not inherited and not _explicit_history_sample(message, now.date()):
                plan._planning_issues.append({'type': 'normalized_implicit_history_window',
                    'detail': '用户未限定历史样本，采用六个完整自然月及本月至今的默认查询范围。'})
                plan.analysisWindow = None
            current_month = now.date().replace(day=1)
            vague_history = re.sub(r'最近(?!\s*(?:[\d零一二两三四五六七八九十半几]|一段|一周))', '', message)
            user_limited = self._has_history_reference(vague_history, now) or bool(re.search(
                r'天内|日内|星期|本周|前\s*[\d一二两三四五六七八九十]+\s*[天日周]|\d+\s*[日号]', message))
            if (plan.analysisWindow and not user_limited and not plan.followUpOfPreviousQuestion
                    and plan.analysisWindow.startDate > shift_month(current_month, -2)
                    and shift_month(current_month, -1) <= plan.analysisWindow.endDate <= now.date()):
                if not future and any(t.tool == 'changes' for t in plan.analyses):
                    plan.analysisWindow = AnalysisWindow(startDate=shift_month(current_month, -2), endDate=now.date())
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
                    if task.months is None and _duration_evidence(message)[0] and not explicit_month_delta(message):
                        raise ValueError("savings.months 未提取用户明确给出的期限")
                    if task.targetAmount is None and re.search(r"(?:存到|达到|目标)\D{0,6}\d", message):
                        raise ValueError("savings.targetAmount 未提取用户明确给出的目标金额")
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

    def _sanitize_conditions(self, plan, message, book_mode, context, *, prune_prior=True):
        issues = sanitize_user_conditions(plan, message, self._normalize_mode(book_mode), context)
        withdrawn = {(issue['metric'], issue['source']) for issue in issues if issue['type'] == 'withdrawn_source_assumption'}
        # On the final pass context already contains newly validated replacements.
        if withdrawn and prune_prior:
            remove_withdrawn_changes(context, withdrawn)
        for issue in issues:
            if issue.get('retirePrevious'):
                field = issue['field']
                context.pop(field, None)
                for state in context.get('scenarios', []):
                    state.pop(field, None)
                for task in context.get('activeAnalyses', []):
                    task.pop(field, None)
        plan._planning_issues.extend(issues)

    def _normalize_query_scopes(self, plan, modes):
        if len(modes) == 1:
            target = next(name for name, (mode, _) in PLANNER_TOOL_BY_NAME.items() if mode in modes)
            foreign = [call for call in plan.toolCalls if call.toolName in PLANNER_TOOL_BY_NAME and call.toolName != target]
            if foreign:
                if any(call.toolName == target for call in plan.toolCalls):
                    # Keep tasks for the requested ledger. A task bound only to
                    # another ledger must not silently acquire different records.
                    plan.toolCalls = [call for call in plan.toolCalls if call not in foreign]
                    removed = {call.id for call in foreign} - {call.id for call in plan.toolCalls}
                    plan.analyses = [task for task in plan.analyses if not task.queryIds or set(task.queryIds) - removed]
                    for task in plan.analyses:
                        task.queryIds = [key for key in task.queryIds if key not in removed]
                else:
                    for call in foreign:
                        call.toolName = target
                        call.reason = '查询当前指定账本并保留本次筛选条件'
                plan._planning_issues.append({'type': 'normalized_ledger_scope', 'tool': target})
        for call in plan.toolCalls:
            call.args.cost_type = self._normalize_cost_type(call.args.cost_type)
            call.args.remark = call.args.remark or ''

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
        if _explicit_history_sample(message, now.date()):
            return
        window = previous.get('activeHistoryWindow', previous['conversation'].get('analysisWindow'))
        current = plan.analysisWindow.model_dump(mode='json') if plan.analysisWindow else None
        if current != window:
            plan.analysisWindow = AnalysisWindow.model_validate(window) if window else None
            plan._planning_issues.append({'type': 'preserved_referent_window',
                'detail': '追问未修改历史时间范围，保留被追问分析的样本窗口。'})

    def _ground_spending_request(self, plan, message):
        """Bind an explicit spending-adjustment goal to its executable task."""
        if plan.intent != 'analyze_orders' or ('个人' in message and '家庭' in message):
            return
        if len({(c.toolName, c.args.cost_type, c.args.remark) for c in plan.toolCalls}) > 1:
            return
        clauses = re.split(r'[，。；！？,;\n]', message)
        goals = {explicit_goal_amount(raw) for clause in clauses if not re.search(r'不是|并非|不想|不用|无需', clause)
                 for raw in re.findall(_MONTHLY_GOAL, clause)} - {None}
        protected = []
        aliases = {name for t in plan.analyses if t.aggregate for name in t.aggregate.filters.excludeCategories}
        referents = ' '.join(plan.understanding.referents)
        for clause in clauses:
            match = re.fullmatch(r'\s*([\w·、和与及]{1,100}?)(?:都|也|继续|要)?(维持原状|保持不变|不能动|不动|保留|不想再省|不能再省)\s*', clause)
            if not match:
                continue
            names = [name for name in re.split(r'[、和与及]', match[1]) if name]
            protected.extend(names)
            # An excluded ledger label can accompany the user's literal when the
            # understanding explicitly binds it to the same protection instruction.
            protected.extend(name for name in sorted(aliases) if name not in names and
                             re.search(re.escape(name) + r'[（(：:\s]*' + re.escape(match[2]), referents))
        adjusting = protected and re.search(r'调整|可削减|还能.{0,4}省|节省空间', message)
        if len(goals) != 1 and not adjusting:
            return
        if len(goals) == 1:
            decision_words = {'budget': r'预算|花费上限', 'purchase': r'买|购买|购置',
                              'savings': r'存到|攒到|攒够|存够|储蓄目标|存款目标|目标(?:金额|余额)',
                              'runway': r'撑多久|承受多久|撑几个月|支撑|承受时长|维持多久|runway',
                              'scenario': r'假设|如果|情景|(?:收入|支出).{0,5}(?:减少|增加|中断|减半)'}
            absent = {name for name, words in decision_words.items() if not re.search(words, message)}
            plan.understanding.evidenceNeeds = [name for name in plan.understanding.evidenceNeeds if name not in absent]
            for field in ('requestedAnswers', 'unknowns'):
                texts = getattr(plan.understanding, field)
                setattr(plan.understanding, field, [text for text in texts if not any(
                    re.search(decision_words[name], text) for name in absent)])
            plan.analyses = [t for t in plan.analyses if t.tool not in absent]
        optimizers = [t for t in plan.analyses if t.tool == 'optimization']
        if not optimizers:
            opt = AnalysisRequest(tool='optimization')
            plan.analyses.append(opt)
            optimizers = [opt]
        if len(optimizers) == 1:
            task = optimizers[0]
            task.protectedCategories = list(dict.fromkeys(task.protectedCategories + protected))[:30]

            if len(goals) == 1:
                amount = next(iter(goals))
                if 0 <= amount <= 1e12:
                    if task.monthlyImprovement != amount:
                        plan._planning_issues.append({'type':'normalized_explicit_condition','field':'monthlyImprovement','value':amount})
                    task.monthlyImprovement = amount
                    if not re.search(r'假设|已经|已减少|少花|支出减少', message):
                        task.changes = [c for c in task.changes if not (
                            c.metric == 'expense' and not c.source and c.amount is not None and -c.amount*c.count == amount)]

    def _decision_goals(self, message, plan, previous, book_mode):
        """Evidence labels request support; decisions need a user goal or a scoped referent."""
        clauses = [c for c in re.split(r'[，。；！？,;\n]', message) if not re.search(
            r'(?:不用|无需|不必|不要|先不|暂不|别)(?:再|帮我)?(?:预测|推演|计算|考虑|评估|分析|做|买|估算|模拟)', c)]
        text = '，'.join(clauses)
        # Quoted ledger labels are selectors, even when a label names a decision.
        text = re.sub(r"""(?:备注|分类|类别|来源|项目)(?:名(?:称)?)?(?:里|中)?(?:写着|包含|含有|含|为|是|叫)?\s*[“‘「"'][^”’」"']*[”’」"']""", '', text)
        temporal_outlook = r'(?:下下?个?月|下个?季度|明年|未来|接下来)'
        forecast = (re.search(r'预测|预计|预估|预期', text)
                    or re.search(temporal_outlook + r'.{0,18}(?:收入|支出|进账|开销|收支|结余)', text))
        horizon = re.search(rf'(?:未来|接下来|今后|往后)\s*(?:{NUMBER}\s*个?月|半年|一年)', text)
        buying = r'(?:购买|购置|买)(?!过|了|完|的)'
        requests = {
            'forecast': forecast,
            'projection': (re.search(r'多月推演|累计.{0,6}结余|逐月预测', text) or
                           (horizon and re.search(r'收支|收入|支出|结余|攒|存|余额', text))),
            'purchase': re.search(r'买得起|买不起|(?:能否|能不能|可以|能|该不该|要不要|是否|可不可以).{0,5}' + buying
                                  + r'|(?:准备|打算|计划|想).{0,14}' + buying + r'|' + buying
                                  + r'.{0,20}(?:吗|合适|划算|可行|足够|够不够|影响)', text),
            'savings': re.search(r'储蓄目标|存款目标|目标(?:余额|存款)|存到|攒到|攒够|存够|多久.{0,8}(?:存|攒)', text),
            'budget': re.search(r'预算.{0,12}(?:还剩|剩余|还够|够不够|超|还可以|还能|[元块]|上限|额度)|'
                                r'(?:核算|规划|计算|制定|安排|设置|检查|看下|查查).{0,5}预算|超预算', text),
            'runway': re.search(r'(?:撑|坚持|支撑|维持|承受|支应).{0,4}(?:多久|多少|几个月|多长|多远|得住)', text),
            'optimization': re.search(r'每个?月.{0,5}多(?:留|存|攒|节省)|优化.{0,6}(?:开支|支出|预算)|'
                                      r'(?:怎么|如何|怎样|可|能).{0,5}(?:调整|节省|削减|压缩|省钱)|节支空间', text),
            'scenario': re.search(r'情景(?:分析|测算|推演|比较)|模拟.{0,8}(?:收支|变化)|'
                                  r'(?:假设|如果|假如|要是).{0,32}(?:增|减|少花|多花|停发|中断|翻倍|归零)', text)
                        or named_percent_changes(text),
        }
        explicit = {tool for tool, supported in requests.items() if supported}
        if not (plan.followUpOfPreviousQuestion and not plan.resetAnalysisContext
                and previous and not previous.get('ambiguous')
                and re.search(r'刚才|上轮|上次|之前那|原来那|原计划|那个|这个数|同样|沿用|继续|再算|重新算', text)):
            return explicit, set()
        scope = {(call.toolName, call.args.cost_type, call.args.remark or '') for call in plan.toolCalls}
        if not scope:
            default = 'search_family_orders' if self._normalize_mode(book_mode) == '家庭版' else 'search_personal_orders'
            scope = {(default, '不限', '')}
        if len(scope) != 1 or [list(next(iter(scope)))] != previous.get('scope'):
            return explicit, set()
        active = previous.get('activeAnalyses') or previous.get('conversation', {}).get('analyses', [])
        if not active and previous.get('targetMonth') and not any(previous.get(k) is not None for k in (
                'months', 'purchaseAmount', 'targetAmount', 'monthlyImprovement', 'budgetAmount')):
            # Legacy forecast histories stored a scoped target before activeAnalyses existed.
            return explicit, {'forecast'}
        return explicit, {row['tool'] for row in active if row.get('tool')}

    def _materialize_requirements(self, plan, message='', previous=None, book_mode='个人版'):
        from app.agent.plan_review import CLAUSES, HISTORY_WORDS, NEGATED, _amount_matches, _direction_request, _explicit_amount, _time_evidence

        if plan.intent != 'analyze_orders':
            return
        self._ground_spending_request(plan, message)
        requirements = set(plan.understanding.evidenceNeeds)
        existing = {task.tool for task in plan.analyses}
        bindings = {(c.toolName, c.args.cost_type, c.args.remark, tuple(c.sourceIds or [c.id]))
                    for c in plan.toolCalls}
        decisions = HISTORY_BASED_TOOLS | {'budget'}
        requested = requirements & {'cashflow', 'data_quality', 'trends', 'changes', 'aggregate', 'income_stability', 'recurring', 'anomalies'}
        explicit, referenced = self._decision_goals(message, plan, previous, book_mode) if len(bindings) <= 1 else (set(), set())
        grounded = explicit | referenced
        if len(explicit) == 1 and not (previous or {}).get('ambiguous'):
            primary = next(iter(explicit))
            # A committed task may already compute an input hypothesis or the monthly outlook.
            covered = ((primary == 'scenario' and existing & {'projection', 'savings', 'runway'})
                       or (primary == 'forecast' and 'projection' in existing))
            if not covered:
                requested.add(primary)
            # A supporting cashflow/trend calculation needs no independent user goal.
            # Conversely, a decision requiring a separate price, budget, target or shock
            # cannot usefully answer this one when both its goal and its input are absent.
            required_input = {'budget': 'budgetAmount', 'savings': 'targetAmount', 'runway': 'changes',
                              'purchase': 'purchaseAmount', 'scenario': 'changes'}
            def unrelated_empty(task):
                if task.tool in grounded:
                    return False
                if primary in {'purchase', 'savings'} and task.tool == 'forecast':
                    # These decisions calculate their own baseline. An independent
                    # forecast needs a requested outlook or a referred previous goal.
                    return True
                if task.tool not in required_input:
                    return False
                value = getattr(task, required_input[task.tool])
                return value is None or value == []
            plan.analyses = [task for task in plan.analyses if not unrelated_empty(task)]
            existing = {task.tool for task in plan.analyses}
        missing_decisions = (requirements & decisions) - existing
        requested |= missing_decisions & grounded
        unsupported = missing_decisions - grounded
        if unsupported:
            plan.understanding.evidenceNeeds = [need for need in plan.understanding.evidenceNeeds if need not in unsupported]
            plan._planning_issues.append({'type': 'ungrounded_decision_needs', 'tools': sorted(unsupported)})
        if requirements & {'sources', 'members', 'remarks'}:
            requested.add('sources')
        if 'quality' in requirements:
            requested.add('data_quality')

        future = bool((existing | requested) & decisions)
        overall = re.search(r"财务|收支|现金流", message) or ("收入" in message and "支出" in message)
        diagnosis = re.search(r"(?:财务|收支|现金流)的?(?:情况|状况|概况|健康|体检|诊断|表现)"
                              r"|(?:整体|总体|全面)(?:财务|收支|现金流)", message)
        direction_only = _direction_request(message)[1] or re.search(r"(?:只|仅|单)(?:想|需|要)?(?:看|算|分析|统计|比较).{0,12}(?:收入|支出|开销)", message)
        filtered = any(c.args.cost_type != '不限' or c.args.remark for c in plan.toolCalls) or any(
            t.aggregate and t.aggregate.filters.model_dump(exclude_defaults=True) for t in plan.analyses)
        if (overall and (diagnosis or 'totals' in requirements) and not direction_only
                and not future and len(bindings) <= 1 and not filtered):
            requested.add('cashflow')
        if plan.output.chart != 'none' and 'aggregate' not in existing | requested and not future:
            requested.add('trends')

        amount_roles = {
            'savings': ('startingBalance', 'targetAmount'),
            'purchase': ('startingBalance', 'purchaseAmount', 'reserveAmount'),
            'runway': ('startingBalance',),
            'budget': ('budgetAmount',),
            'optimization': ('monthlyImprovement',),
        }
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
        for tool in sorted(requested - existing, key=lambda name: (name not in decisions, name)):
            if len(plan.analyses) >= 12:
                raise ValueError('请求的证据超过十二项分析，请合并重复任务并保留必要的决策与查询绑定。')
            args = {'metric': 'income' if tool == 'forecast' and plan.output.metric == 'all' else plan.output.metric}
            if tool in {'savings', 'projection'}:
                horizon_text = re.sub(rf"每(?:隔)?(?:{NUMBER}\s*个?月|半年|一年)", "", message)
                _, durations = _time_evidence(horizon_text, today)
                if len(durations) == 1:
                    count = next(iter(durations))
                    if count > 0:
                        args['months'] = count
            plan.analyses.append(AnalysisRequest(tool=tool, **args))
        # The user supplies the same typed conditions whether the model chose the
        # calculation itself or the planner had to add it. Multiple alternatives
        # require an explicit binding for a price or balance to a particular task.
        if len(bindings) <= 1:
            current_clauses = [clause for clause in CLAUSES.split(message)
                               if not NEGATED.search(clause) and not HISTORY_WORDS.search(clause)
                               and not _explicit_history_sample(clause, today)]
            for tool, fields in amount_roles.items():
                tasks = [task for task in plan.analyses if task.tool == tool]
                if len(tasks) != 1:
                    continue
                for field in fields:
                    if getattr(tasks[0], field) is not None:
                        continue
                    values = {v for clause in current_clauses for v in _amount_matches(clause, field)}
                    if len(values) == 1 and _explicit_amount(message, field, next(iter(values))):
                        setattr(tasks[0], field, values.pop())

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
            for call in restored:
                call.args.cost_type = self._normalize_cost_type(call.args.cost_type)
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
        targets = {row.get('targetMonth') for row in active} - {None}
        intent_text = ' '.join([plan.understanding.objective, *plan.understanding.requestedAnswers])
        explains_active = (any(task.targetMonth in targets for task in plan.analyses if task.targetMonth)
                           or bool(re.search(r'预测|预估|测算|推演', intent_text)))
        support_only = not any(task.tool in HISTORY_BASED_TOOLS or task.tool == 'budget' for task in plan.analyses)
        if plan.intent == 'analyze_orders' and active and same_window and (sources_only or (explains_active and support_only)):
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
