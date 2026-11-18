"""One bounded semantic review of a logical plan before any ledger query is expanded."""

import asyncio
import json
import re
from datetime import date, datetime
from time import perf_counter
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError
from pydantic_core import to_jsonable_python

from app.agent.analysis_context import DECISION_TOOLS
from app.core.config import get_settings
from app.core.llm import get_structured_model
from app.schemas.plan import AgentPlan, AnalysisRequest, AnalysisWindow
from app.tools.financial_analysis import ANALYSIS_TOOLS, shift_month
from app.tools.order_search import normalize_cost_type


class PlanReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analyses: list[AnalysisRequest] = Field(min_length=1, max_length=12)
    analysisWindow: AnalysisWindow | None
    answerInstruction: str = Field(max_length=600)
    adequate: StrictBool
    issues: list[Annotated[str, Field(max_length=200)]] = Field(max_length=5)


PROMPT = """你核对账本分析计划的可执行性。现在尚未查询账本；只返回 schema 要求的 JSON 对象。
逐项核对用户问题、理解中的 objective/requestedAnswers、对话指代和本轮已有查询。假想实际运行这些工具，检查结果是否足以回答每一项，而不是复述目标。
adequate 表示原计划是否足够。无论是否足够，analyses 都给出修正后的完整任务列表，保留原本正确的任务、指标与条件。answerInstruction 只说明如何回答，不能给账本金额、捏造结论或代替执行参数。
未来指定单月的收入、支出或结余须用 forecast 的相应 metric 与 targetMonth；连续多月推演可用 projection。trends 只能展示历史观测。analysisWindow 是要查询的历史证据窗口，不是预测目标月；没有明确限定参考区间时可为 null。条件未提供时不要自拟目标月或预测期限。仅指定下月的默认值可由执行端补齐。
按人/类别/来源/月份等交叉核算、条件筛选或对比，使用 aggregate.groupBy / filters / periods；逐一核对筛选方向、排除项、统计金额和起止日期。比较变化需区分历史记录与用户假设。需要来源或备注依据时读取账本中已有的证据。
优化时 monthlyImprovement 是用户想每月多留的金额，不能当成已发生的支出减少；protectedCategories 要保留。没提供余额可先算已有历史收支，明确购买/储蓄决策欠缺的条件。禁止自行补余额、储备金、目标金额或工作安排。
追问只沿用同账本同筛选范围的既有条件；新问题不能照搬另一情景。previousContext 是范围明确的解析条件；旧助手回答不证明余额或交易。只使用 logicalQueryIds 给出的键；空数组沿用正常默认绑定，不创建查询或更改权限。
工具清单："""


NUMBER = r"(?:\d+(?:,\d{3})*(?:\.\d+)?万?|[零一二两三四五六七八九十百千万亿]+)"
ROLES = {
    "startingBalance": r"可动用(?:余额|现金|资金)?|可用(?:余额|现金|资金)|余额|存款|手头(?:有|现金)|现有现金|现在(?:已)?有|目前(?:已)?有",
    "reserveAmount": r"应急(?:金|钱|储备)?|储备(?:金|底线)?|备用金|保底金",
    "targetAmount": r"储蓄目标|存款目标|目标(?:金额|余额)?|存到|攒到|攒够|存够",
    "purchaseAmount": r"购买(?:价|金额)?|售价|标价|买(?:一[个台辆部件])?",
    "budgetAmount": r"预算(?:上限|金额|额度)?",
    "monthlyImprovement": r"(?:每个?月)?(?:额外)?多(?:留(?:下)?|存(?:下)?|攒(?:下)?|结余|节省)",
}
NEGATED = re.compile(r"没有|没说|没提供|未知|不是|并非|不会|并不|不(?:用|必|需|留|买|考虑|清楚|确定)|无需")
CLAUSES = re.compile(r"[，。；！？;\n]|(?<!\d),|,(?!\d)")
INCREASE = re.compile(r"增加|新增|翻倍|多(?:赚|花|收)|上涨")
DECREASE = re.compile(r"减少|削减|缩减|少(?:花|收|赚|一半)|减半|停(?:了|发|止)|中断|归零|下降|降低|取消")
HISTORY_WORDS = re.compile(r"过去|最近|回顾|历史|参考|以往|近(?=[零一二两三四五六七八九十\d])")
FUTURE_WORDS = re.compile(r"未来|预测|推演|预计|接下来|延长|期限|目标|计划|想在|想")


def _number(raw):
    # Keep one definition of Chinese and Arabic monetary literals.
    from app.agent.planner import explicit_goal_amount
    return explicit_goal_amount(raw.replace(",", ""))


def _amount_matches(clause, field):
    role = "(?:" + ROLES[field] + ")"
    joins = r"[\s的是为约有现共需还要至少到达留设成作]*"
    patterns = [rf"{role}{joins}({NUMBER})", rf"({NUMBER})\s*(?:元|块)?{joins}{role}"]
    if field == "purchaseAmount" and re.search(r"买|购", clause):
        patterns.append(rf"(?:花费?|付){joins}({NUMBER})")
    if field == "startingBalance":
        # A savings target uses a future balance, not available cash.
        clause = re.sub(r"(?:目标(?:余额|存款)|存款目标|希望余额|想要余额)", "目标款项", clause)
    return {_number(raw) for pattern in patterns for raw in re.findall(pattern, clause)} - {None}


def _retracts(question, field, previous_value):
    if previous_value is None:
        return False
    for clause in CLAUSES.split(question):
        if NEGATED.search(clause) and re.search(ROLES[field], clause):
            excluded = _amount_matches(clause, field)
            if not excluded or previous_value in excluded:
                return True
    return False


def _amount_supported(question, field, value, prior):
    positive, negative = set(), set()
    for clause in CLAUSES.split(question):
        if NEGATED.search(clause):
            negative.update(_amount_matches(clause, field))
        else:
            positive.update(_amount_matches(clause, field))
    if positive:
        return value in positive and value not in negative
    return prior.get(field) == value and not _retracts(question, field, value)


def _explicit_amount(question, field, value):
    return _amount_supported(question, field, value, {})


def _scope_for(task, plan, mode):
    if not plan.toolCalls:
        tool = "search_family_orders" if mode == "家庭版" else "search_personal_orders"
        return {(tool, "不限", "", None, None, None)}
    return {(q.toolName, normalize_cost_type(q.args.cost_type), q.args.remark or "", q.args.year, q.args.month, q.args.day)
            for q in plan.toolCalls if task is None or not task.queryIds or q.id in task.queryIds}


def _prior_for(task, plan, mode, previous):
    if not plan.followUpOfPreviousQuestion or plan.resetAnalysisContext or previous.get("ambiguous"):
        return {}
    scope = {key[:3] for key in _scope_for(task, plan, mode)}
    return previous if len(scope) == 1 and sorted(scope) == sorted(tuple(r) for r in previous.get("scope", [])) else {}


def _same_change(change, prior):
    return all(prior.get(k, default) == getattr(change, k)
               for k, default in (("metric", None), ("source", ""), ("amount", None), ("percent", None), ("count", 1)))


def _change_supported(change, question, prior):
    if any(_same_change(change, old) for old in prior.get("changes", [])):
        return True
    clauses = CLAUSES.split(question)
    for i, clause in enumerate(clauses):
        delta = change.amount if change.amount is not None else change.percent
        if NEGATED.search(clause):
            continue
        if not delta:
            if "不变" in clause or "保持原样" in clause:
                return True
            continue
        direction = INCREASE if delta > 0 else DECREASE
        if not direction.search(clause):
            continue
        context = clause
        # A following unit-rate clause can explain the user's stated number of lessons/visits.
        if i + 1 < len(clauses) and clauses[i+1].lstrip().startswith(("每", "按每")):
            context += "，" + clauses[i+1]
        if change.source and change.source not in context:
            previous_sources = {c.get("source") for c in prior.get("changes", []) if c.get("metric") == change.metric}
            if previous_sources != {change.source} or not re.search(r"这项|该项|这个来源|它", context):
                continue
        if change.metric == "income" and "支出" in context and "收入" not in context:
            continue
        if change.metric == "expense" and "收入" in context and "支出" not in context:
            continue
        numbers = {_number(n) for n in re.findall(NUMBER, context)}
        if change.count != 1 and change.count not in numbers:
            continue
        if change.amount is not None and abs(change.amount) in numbers:
            return True
        if change.percent is not None:
            if abs(change.percent) == 50 and "半" in context:
                return True
            if change.percent == -100 and re.search(r"所有|全部|全都|归零", context) and re.search(r"停|中断|归零", context):
                return True
            if change.percent == 100 and "翻倍" in context:
                return True
            if abs(change.percent) in numbers and re.search(r"[%％]|百分之", context):
                return True
    return False


def _check_conditions(analyses, plan, question, mode, previous, *, strip):
    issues = []
    for task in analyses or [None]:
        prior = _prior_for(task, plan, mode, previous)
        for field in ROLES:
            value = getattr(task, field, None)
            retire = strip and _retracts(question, field, prior.get(field))
            unsupported = value is not None and not _amount_supported(question, field, value, prior)
            if retire or unsupported:
                issues.append({"type": "unsupported_user_condition", "tool": task.tool if task else "context",
                               "field": field, **({"retirePrevious": True} if retire else {})})
                if strip and unsupported:
                    setattr(task, field, None)
        if not task:
            continue
        kept = [change for change in task.changes if _change_supported(change, question, prior)]
        if len(kept) != len(task.changes):
            issues.append({"type": "unsupported_user_condition", "tool": task.tool, "field": "changes"})
            if strip:
                task.changes = kept
    return issues


def sanitize_user_conditions(plan, question, book_mode, previous_context):
    """Remove unsupported scalar/what-if inputs, including on a failed review path."""
    return _check_conditions(plan.analyses, plan, question, book_mode, previous_context or {}, strip=True)


def _temporal_role(clause, match):
    before = clause[:match.start()]
    history = max((m.start() for m in HISTORY_WORDS.finditer(before)), default=-1)
    future = max((m.start() for m in FUTURE_WORDS.finditer(before)), default=-1)
    if re.match(r"\s*(?:的)?(?:历史|样本)", clause[match.end():]):
        return "history"
    if history > future:
        return "history"
    return "future" if future >= 0 or re.match(r"\s*(?:之)?内", clause[match.end():]) else "unspecified"


def _time_evidence(question, today):
    targets, durations = set(), {"future": set(), "unspecified": set()}
    month_date = rf"(?:(\d{{4}}\s*[-年/.]|明年|今年|去年)\s*)?({NUMBER})\s*月"
    for clause in CLAUSES.split(question):
        for match in re.finditer(month_date, clause):
            raw_year, raw_month = match.groups()
            month = _number(raw_month)
            if not month or month != int(month) or not 1 <= month <= 12 or _temporal_role(clause, match) == "history":
                continue
            if raw_year and raw_year[:1].isdigit():
                year = int(raw_year[:4])
            elif raw_year:
                year = today.year + {"明年": 1, "今年": 0, "去年": -1}[raw_year]
            else:
                year = today.year + (month < today.month)
            targets.add(f"{year:04}-{int(month):02}")
        for match in re.finditer(r"(?<!\d)(\d{4})[-/.](0?[1-9]|1[0-2])(?!\d)", clause):
            if _temporal_role(clause, match) != "history":
                targets.add(f"{int(match[1]):04}-{int(match[2]):02}")
        for match in re.finditer(r"下下?个?月|这个月|本月", clause):
            if _temporal_role(clause, match) != "history":
                targets.add(shift_month(today, match[0].count("下")).strftime("%Y-%m"))
        for match in re.finditer(rf"({NUMBER})\s*个\s*月|半年|一年", clause):
            role = _temporal_role(clause, match)
            if role == "history":
                continue
            count = {"半年": 6, "一年": 12}.get(match[0]) or _number(match[1])
            if count is not None and 0 <= count <= 600 and int(count) == count:
                durations[role].add(int(count))
                if clause[match.end():].lstrip().startswith("后"):
                    targets.add(shift_month(today, int(count)).strftime("%Y-%m"))
    return targets, durations["future"] or durations["unspecified"]


def _binding_issue(decision, plan, logical_ids, question, today, mode, previous):
    all_scopes = _scope_for(None, plan, mode)
    targets, durations = _time_evidence(question, today)
    for task in decision.analyses:
        if set(task.queryIds) - logical_ids:
            return "unknown_query_id"
        scope = _scope_for(task, plan, mode)
        original = [old for old in plan.analyses if old.tool == task.tool]
        if original and not any(scope == _scope_for(old, plan, mode) for old in original):
            return "changed_query_scope"
        if task.tool in DECISION_TOOLS and (len(scope) != 1 or (not original and len(all_scopes) > 1)):
            return "ambiguous_decision_scope"
        issues = _check_conditions([task], plan, question, mode, previous, strip=False)
        if issues:
            return "unsupported_" + issues[0]["field"]
        prior = _prior_for(task, plan, mode, previous)
        prior_target = prior.get("budgetTargetMonth" if task.tool == "budget" else "targetMonth")
        target = task.targetMonth
        if target:
            if task.tool in {"forecast", "projection"} and target < today.strftime("%Y-%m"):
                return "unsupported_targetMonth"
            defaults = {shift_month(today, 1).strftime("%Y-%m")} if task.tool in {"forecast", "projection"} else set()
            valid = targets or ({prior_target} if prior_target else defaults)
            if target not in valid:
                return "unsupported_targetMonth"
        if task.months is not None and task.months not in (durations or {prior.get("months")}):
            return "unsupported_months"
    return None


def _call_metadata(response):
    metadata = getattr(response, "response_metadata", {}) or {}
    detail = {}
    for source, target in (("prompt_eval_count", "promptTokens"), ("eval_count", "outputTokens")):
        value = metadata.get(source)
        if type(value) is int and 0 <= value <= 10**8:
            detail[target] = value
    reason = metadata.get("done_reason", metadata.get("finish_reason"))
    if reason in ("stop", "length", "tool_calls", "load", "unload"):
        detail["doneReason"] = reason
    return detail


def _parse(response):
    text = re.sub(r"<think>.*?</think>", "", str(response.content), flags=re.S).strip()
    if match := re.fullmatch(r"\x60{3}(?:json)?\s*(.*?)\s*\x60{3}", text, flags=re.S | re.I):
        text = match[1]
    return PlanReviewDecision.model_validate_json(text)


class PlanReviewer:
    async def review(self, question, history, plan: AgentPlan, today: date, book_mode, previous_context):
        today = today.date() if isinstance(today, datetime) else today
        logical_ids = {call.id for call in plan.toolCalls if call.id} if plan.toolCalls else {"ledger"}
        previous = previous_context or {}
        payload = {"question": question, "history": history[-4000:], "bookMode": book_mode, "today": today.isoformat(),
                   "previousContext": to_jsonable_python(previous), "logicalQueryIds": sorted(logical_ids),
                   "plan": plan.model_dump(mode="json")}
        system = PROMPT + json.dumps(ANALYSIS_TOOLS, ensure_ascii=False) + "\nJSON schema：" + json.dumps(
            PlanReviewDecision.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
        attempts = []
        audit = {"status": "unavailable", "attempts": attempts}
        for attempt in range(2):
            started = perf_counter()
            call = {}
            attempts.append(call)
            try:
                response = await asyncio.wait_for(get_structured_model(PlanReviewDecision).ainvoke([
                    ("system", system), ("human", json.dumps(payload, ensure_ascii=False))]),
                    timeout=get_settings().planner_timeout_seconds)
                call.update(_call_metadata(response))
                decision = _parse(response)
                issue = _binding_issue(decision, plan, logical_ids, question, today, book_mode, previous)
                if issue:
                    return None, {**audit, "error": issue}
                return decision, {**audit, "status": "reviewed", "decision": decision.model_dump(mode="json")}
            except ValidationError as exc:
                call["error"] = "invalid_structure"
                errors = exc.errors(include_input=False, include_context=False, include_url=False)
                if attempt:
                    return None, {**audit, "error": "invalid_structure"}
                payload["corrections"] = [{"field": ".".join(map(str, e["loc"]))[:100], "type": e["type"][:60]}
                                          for e in errors[:5]]
            except Exception as exc:
                return None, {**audit, "error": type(exc).__name__}
            finally:
                call["durationMs"] = round((perf_counter() - started) * 1000)
