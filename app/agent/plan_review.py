"""One bounded semantic review of a logical plan before any ledger query is expanded."""

import asyncio
import json
import re
from datetime import date, datetime
from time import perf_counter
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError
from pydantic_core import to_jsonable_python

from app.agent.analysis_context import DECISION_TOOLS, source_direction_of
from app.agent.conversation_context import compact_user_context
from app.agent.report_context import build_report_context
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
        patterns.extend([rf"(?:花费?|付){joins}({NUMBER})",
                         rf"(?:拿出?|用|支付){joins}({NUMBER})\s*(?:元|块(?:钱)?)\s*(?:直接|去|来|用于)?(?:买|购)"])
    if field == "startingBalance":
        # A past snapshot is not proof of present cash. When the clause explicitly
        # transitions back to the present, examine only that current portion.
        past = list(re.finditer(r"去年|前年|上(?:上)?个?月|以前|当时|曾经|过去|截至\d{4}年", clause))
        if past:
            current = re.search(r"现在|目前|当前|如今|今天", clause[past[-1].end():])
            if not current:
                return set()
            clause = clause[past[-1].end()+current.start():]
        # A savings target uses a future balance, not available cash.
        clause = re.sub(r"(?:目标(?:余额|存款)|存款目标|希望余额|想要余额)", "目标款项", clause)
    matches = (match for pattern in patterns for match in re.finditer(pattern, clause))
    if field == "purchaseAmount":
        # In “预算七百元买设备”, the number limits the budget. The verb that
        # follows the amount does not convert a balance or budget into a price.
        other_role = re.compile(r"(?:" + "|".join(ROLES[f] for f in ("budgetAmount", "startingBalance", "reserveAmount"))
                                + r")" + joins + r"$")
        matches = (m for m in matches if not other_role.search(clause[:m.start(1)]))
    # In 买一台电脑 the number counts objects. Only a price can supply a purchase amount.
    return {_number(m[1]) for m in matches if field != "purchaseAmount"
            or not re.match(r"\s*[台个辆部件套只张本份]", clause[m.end(1):])} - {None}


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


# Retractions describe the assumption to remove, not a new numerical condition.
WITHDRAW = re.compile(r"不再(?:假设|假定|考虑|按)|(?:撤回|撤销|(?:取消|去掉|移除)(?=.{0,100}(?:假设|情景)))(?:之前|原来|原先|刚才|上次|上轮)?(?:的)?(?:关于)?")
KEEP = re.compile(r"不(?:想|要|用|必|会)?(?:再)?(?:改变|调整|改动|取消|撤回|撤销|移除|去掉)")
CORRECTION = re.compile(r"(?:不是|并非)([^，,。；！？;]+?)(?:[，,]\s*(?:而?是)|而是)")


def _deadline_retractions(clause):
    deadline = r"(?:截止(?:日期|时间|期限)?|期限|时限)"
    action = (r"(?:取消|撤销|撤回|去掉|移除|不按|不再(?:沿用|保留|按)|"
              r"不(?:再)?(?:设定|设置|设|限定|限制|要求))")
    negation = r"(?:不是|并非|不|无需|别|勿|(?:并|尚)?未|没有|没)(?:再)?(?:打算|准备|考虑|想|要|用|必|会|能){0,2}(?:再)?\s*$"
    patterns = (rf"{action}.{{0,24}}?{deadline}|(?:没有|无)(?:固定|明确)?{deadline}|"
                rf"{deadline}\s*(?:(?:这次|现在|也|一并|都|就)\s*)*{action}|不(?:再)?限期")
    return [match for match in re.finditer(patterns, clause) if not re.search(negation, clause[:match.start()])]


def _retracts_deadline(question):
    return any(_deadline_retractions(clause) for clause in CLAUSES.split(question))


def _scenario_clauses(question):
    # Isolate the replacement operand in “不是 A，是 B 减半” with or without a comma.
    text = CORRECTION.sub(lambda m: f"不是{m[1]}，", question)
    return [clause.strip() for clause in CLAUSES.split(text)]


def _negated_change(clause):
    return NEGATED.search(clause) or WITHDRAW.search(clause) or KEEP.search(clause)


_SOURCE_RETRACTION_NONCOMMAND = re.compile(
    r"(?:假如|假设|假定|如果|倘若|要是|是否|之前|先前|上(?:一)?[轮次]|以前|曾经|过去|当时|"
    r"提到|提过|说过|听说|回顾|引述|讨论)"
    r"|(?:别|请勿|无[需须]|没(?:有)?|未(?:曾)?|不(?:是(?:要)?|打算|准备|考虑|想|要|用|必|会)?)(?:再)?\s*$"
    r"""|["'‘“「『]\s*$"""
)
_SOURCE_RETRACTION_TAIL = (
    rf"$|(?:这[个项种]|该|的)?(?:假设|情景)|(?:可能|将会?|会)?"
    rf"(?:减(?:少|半|低|(?={NUMBER}))|增(?:加|长)|多(?:花|收|赚|(?={NUMBER}))|"
    rf"少(?:花|收|赚|半|(?={NUMBER}))|上涨|下降|停(?:发|止|了))|重新|再算|算|推演"
)
_SOURCE_NORMAL_TAIL = r"(?:恢复|回到)(?:原本|原先|正常|原状|原来|原有)"


def _source_retraction_reference(subject, name_pattern, tail):
    return re.match(
        rf"""^["'‘“「『]?(?P<name>{name_pattern})["'’”」』]?\s*(?:的)?"""
        rf"""(?P<role>收入|支出)?["'’”」』]?\s*(?:{tail})""", subject)


def _withdrawn_changes(question, previous):
    """Find scoped (metric, source) keys explicitly excluded by the user."""
    keys = {(c["metric"], c["source"]) for c in previous if c.get("source")}
    roles = {"income": "收入", "expense": "支出"}
    withdrawn = set()
    for clause in _scenario_clauses(question):
        if KEEP.search(clause):
            continue
        cue = WITHDRAW.search(clause) or re.search(r"(?:按|用)正常(?:的)?", clause)
        if not cue and CORRECTION.search(question):
            cue = re.match(r"(?:不是|并非)", clause)
        if cue and _SOURCE_RETRACTION_NONCOMMAND.search(clause[:cue.start()]):
            continue
        subject = clause[cue.end():].strip() if cue else clause
        tail = f"{_SOURCE_NORMAL_TAIL}|{_SOURCE_RETRACTION_TAIL}" if cue else _SOURCE_NORMAL_TAIL
        matches = set()
        for metric, source in keys:
            role = roles[metric]
            name = source.removesuffix(role) or source
            reference = _source_retraction_reference(subject, re.escape(name), tail)
            if reference and reference["role"] in (None, role):
                matches.add((metric, source))
        if not matches:
            reference = _source_retraction_reference(subject, r"[\w·][\w· -]*?", tail)
            if reference:
                alias = reference["name"].strip()
                # Resolve the alias against confirmed scenario inputs in this scope.
                matches = {(metric, source) for metric, source in keys if alias in source
                           and reference["role"] in (None, roles[metric])}
        if len(matches) == 1:
            withdrawn.update(matches)
    return withdrawn


def named_percent_changes(question):
    """Read a literal subject and percentage from a single directed change clause."""
    result = []
    for clause in _scenario_clauses(question):
        if _negated_change(clause):
            continue
        # A condition may follow an introductory phrase ("未来三个月如果…").
        parts = re.split(r"如果|假如|假设|假定|要是", clause)
        clause = parts[-1].strip()
        if re.match(r"减少|降低|下降|缩减|减半|增加|上涨|少一半", clause):
            if len(parts) < 2 or not parts[-2].strip():
                continue
            clause = parts[-2].strip() + clause
        clause = re.sub(r"^(?:那么?|改为|改成|我的?|下个?月|未来\s*(?:" + NUMBER + r")\s*个月)+", "", clause)
        match = re.match(
            rf"(?P<subject>[\w·'‘’「」“”]{{1,100}}?)(?:预计|可能|将会|将|会|大概|大约)*\s*"
            rf"(?P<verb>减少|降低|下降|缩减|少|减|增加|上涨|多)\s*"
            rf"(?P<rate>一半|半|百分之{NUMBER}|{NUMBER}\s*[%％])", clause)
        if not match:
            continue
        subject = match["subject"].strip("'‘’「」“”")
        direction = re.search(r"(收入|支出)$", subject)
        metric = {"收入": "income", "支出": "expense"}[direction[1]] if direction else None
        source = (subject[:direction.start()] if direction else subject).rstrip("的").strip("'‘’「」“”")
        if not source or re.fullmatch(r"(?:所有|全部|整体|总体|总|每月|每个月|这项|该项|这个来源|它)+", source):
            continue
        raw = match["rate"].replace("百分之", "").rstrip("%％").strip()
        rate = 50 if "半" in raw else _number(raw)
        if rate is not None:
            percent = rate if match["verb"] in {"增加", "上涨", "多"} else -rate
            result.append((source, metric, percent))
    return result


def explicit_month_delta(question):
    matches = [(verb, _number(raw)) for clause in CLAUSES.split(question) if not NEGATED.search(clause)
               for verb, raw in re.findall(rf"(推迟|延长|延后|提前|缩短)\s*({NUMBER})\s*个?月", clause)]
    if len(matches) == 1:
        verb, count = matches[0]
        if count is not None and count == int(count) and 0 < count <= 599:
            return int(count) * (-1 if verb in {"提前", "缩短"} else 1)
    return None


def _change_supported(change, question, prior, withdrawn):
    if (change.metric, change.source) not in withdrawn and any(_same_change(change, old) for old in prior.get("changes", [])):
        return True
    clauses = _scenario_clauses(question)
    for i, clause in enumerate(clauses):
        delta = change.amount if change.amount is not None else change.percent
        if _negated_change(clause):
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
        named = named_percent_changes(clause)
        if named and change.amount is not None and not re.search(r"元|块|￥|¥", context):
            continue
        observed = {source_direction_of(name, prior) for name, role, rate in named
                    if role in (None, change.metric) and rate == change.percent}
        matched_observation = (change.source, change.metric) in observed
        if named and change.percent is not None and not matched_observation and not any(
                (change.source == name or change.source == name + {"income": "收入", "expense": "支出"}[change.metric])
                and metric in (None, change.metric) and change.percent == percent for name, metric, percent in named):
            continue
        if change.source and change.source not in context and not matched_observation:
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


def calendar_context(today):
    return {label: shift_month(today.replace(day=1), offset).strftime("%Y-%m")
            for label, offset in (("本月", 0), ("上个月", -1), ("上上个月", -2))}


DIRECTION_LABELS = {'收入': 'income', '进账': 'income', '入账': 'income',
                    '支出': 'expense', '开销': 'expense', '开支': 'expense', '花费': 'expense', '花销': 'expense'}
_DIRECTION = '|'.join(DIRECTION_LABELS)
_CATEGORY_LITERAL = re.compile(
    rf"(?:分类|类别)(?:名称|名)?\s*(?:(?:为|是|叫做?|叫作|等于|[:：=])\s*[‘“'\"「]?|[‘“'\"「])"
    rf"({_DIRECTION})(?=$|[’”'\"」的账类，。；！？,;？\s])"
    rf"|[‘“'\"「]({_DIRECTION})[’”'\"」]\s*(?:这个|这一)?(?:分类|类别)")
_EXCLUDED_DIRECTION = re.compile(rf"(?:排除|剔除|不含|不包括|不计入)\s*({_DIRECTION})(?=$|[后外，。；！？，,;\s])")


def _direction_request(question):
    # A literal category name provides no evidence about the sign of its entries.
    literal = {a or b for a, b in _CATEGORY_LITERAL.findall(question)}
    text = _CATEGORY_LITERAL.sub('', question)
    if re.search(r'收支|净额|净现金流|净收入|净支出|净流[入出]|结余|收与支', text):
        return literal, None
    excluded = {DIRECTION_LABELS[m] for m in _EXCLUDED_DIRECTION.findall(text)}
    text = _EXCLUDED_DIRECTION.sub('', text)
    positive = {direction for word, direction in DIRECTION_LABELS.items() if word in text}
    choices = (positive or {'income', 'expense'}) - excluded
    return literal, next(iter(choices)) if len(choices) == 1 else None


_FILTER_DIMENSIONS = {
    "includeCategories": "分类|类别", "excludeCategories": "分类|类别",
    "includeMembers": "成员|用户", "excludeMembers": "成员|用户",
    "includeSources": "来源", "excludeSources": "来源",
    "remarkContains": "备注", "excludeRemarks": "备注",
}
_UNRESTRICTED = {"all", "any", "none", "null", "n/a", "*", "全部", "所有", "不限", "无限制", "无筛选", "不限制"}


def _placeholder_is_literal(value, dimension, question):
    """Require a naming/text predicate, so '全部成员' does not name a member."""
    name = re.escape(value)
    kind = rf"(?:{dimension})"
    quoted = rf"""[“「『"']{name}[”」』"']"""
    end = r"(?=$|[的、，,。；;？！?\s])"
    named = rf"(?:名称|名字|名)?\s*(?:叫作?|叫做|命名为|等于|为|是|包含|含有|含|写着|[:：=])\s*"
    return bool(re.search(rf"{kind}\s*(?:{named}(?:{quoted}|{name}{end})|{quoted})|{quoted}\s*(?:这个|这一)?{kind}", question))


def unrequested_future_aggregate(task, plan, question, today):
    """Recorded totals over wholly future periods cannot answer an outlook."""
    if task.tool != "aggregate" or not task.aggregate or not task.aggregate.periods:
        return False
    if any(p.startDate <= today for p in task.aggregate.periods):
        return False
    if not any(t.tool in {"forecast", "projection"} for t in plan.analyses):
        return False

    # Attribute "已记" to its own date/request. A historical sample joined to
    # a future forecast is not permission to aggregate future ledger records.
    for clause in re.split(r"[，。；！？,;\n]|预测|预计|预估|推演|估算", question):
        if not re.search(r"已(?:经)?(?:记|录入|登记|入账|安排)|预订|预定|预约|待扣款|已排期", clause):
            continue
        future = re.search(r"未来|接下来|今后|明年|明天|下(?:下?个?)?(?:月|周|季度)|下半年", clause)
        if future and not re.search(r"过去|以往|历史|去年|上个?月", clause[future.end():]):
            return False
        # A named month must actually intersect one of this task's future periods.
        for match in re.finditer(rf"(?:(\d{{4}})\s*[-年/]\s*)?({NUMBER})\s*月", clause):
            month = _number(match[2])
            if not month or month != int(month) or not 1 <= month <= 12:
                continue
            year = int(match[1]) if match[1] else today.year + (month < today.month)
            key = (year, int(month))
            if any((p.startDate.year, p.startDate.month) <= key <= (p.endDate.year, p.endDate.month)
                   for p in task.aggregate.periods):
                return False
        if (re.search(r"查|列出|核对|查看|统计|汇总", clause) and re.search(r"预订|预定|预约|待扣款|已排期", clause)
                and not re.search(r"过去|以往|历史|去年|上个?月|\d+\s*月", clause)):
            return False
    return True


def canonicalize_aggregate_defaults(filters, question, prior_filters=None):
    """Normalize empty native-schema fillers before planning or examining a review diff."""
    changed = []
    prior_filters = prior_filters or {}
    for field, dimension in _FILTER_DIMENSIONS.items():
        current = getattr(filters, field)
        values = [current] if isinstance(current, str) else current
        prior = prior_filters.get(field, ())
        inherited = [prior] if isinstance(prior, str) else prior
        allowed = [name for name in values if name.casefold() not in _UNRESTRICTED
                   or _placeholder_is_literal(name, dimension, question)
                   or name in inherited]
        canonical = (allowed[0] if allowed else "") if isinstance(current, str) else allowed
        if canonical != current:
            setattr(filters, field, canonical)
            changed.append(field)
    # Bounds apply to absolute transaction amounts. A zero floor never removes
    # a row, whereas a zero ceiling genuinely selects zero-amount transactions.
    if filters.minAmount == 0:
        filters.minAmount = None
        changed.append("minAmount")
    zero = r"(?:0+(?:\.0+)?|零)\s*(?:元|块)?"
    if filters.maxAmount == 0 and not (prior_filters.get("maxAmount") == 0 or
            re.search(rf"(?:零金额|(?:金额|上限)(?:为|是|等于|不超过|至多|最多|小于等于|[≤=])\s*{zero}(?![\d.])"
                      rf"|(?:不超过|至多|最多|小于等于|≤)\s*{zero}(?![\d.])|{zero}(?:及以下|以下))", question)):
        filters.maxAmount = None
        changed.append("maxAmount")
    return changed


def _referenced_aggregate_filters(question, prior, task):
    if not re.search(r"(?:沿用|保留|继续|仍按|还是按)[^，。；,;]{0,12}(?:筛选|过滤)", question):
        return {}
    rows = [row for row in prior.get("conversation", {}).get("analyses", []) if row.get("tool") == "aggregate"
            and (not row.get("queryIds") or not task.queryIds or set(row["queryIds"]) == set(task.queryIds))]
    if len(rows) != 1:
        return {}
    return (rows[0].get("aggregate") or {}).get("filters", {})


def _aggregate_constraints(task, plan, question, prior, mode, *, strip, single_aggregate, literal_categories, direction):
    if not task.aggregate:
        return []
    issues = []
    filters = task.aggregate.filters
    inherited_filters = _referenced_aggregate_filters(question, prior, task)
    normalized = canonicalize_aggregate_defaults(filters, question, inherited_filters)
    if strip:
        issues.extend({"type": "normalized_aggregate_default", "tool": "aggregate", "field": field} for field in normalized)
    for field in ("includeCategories", "excludeCategories"):
        values = getattr(filters, field)
        allowed = [v for v in values if v not in DIRECTION_LABELS or v in literal_categories]
        if values != allowed:
            issues.append({"type": "unsupported_user_condition", "tool": "aggregate", "field": field})
            if strip:
                setattr(filters, field, allowed)
    if len({s[:3] for s in _scope_for(None, plan, mode)}) != 1:
        return issues
    exclude = re.compile(r"(?:排除|剔除|不含|不包括|不计入)[^，。；！？,;\n]*?(?:后|以外|之外|(?=[，。；！？,;\n]|$))")
    # Recover a literal category list only when its boundary and dimension are
    # explicit. Other selectors (members, sources or remarks) keep their own meaning.
    literal = re.search(r"(?:排除|剔除|不含|不包括|不计入)([^，。；！？,;\n]{1,120}?)(?:后|以外|之外)", question)
    if literal and not re.search(r"分类|类别", literal[1]) and ("category" in task.aggregate.groupBy or filters.excludeCategories):
        names = [v.strip(" ‘“「」”’") for v in re.split(r"[、和与及]", literal[1])]
        if (all(re.fullmatch(r"[\w·-]{1,30}", v) for v in names)
                and not re.search(r"备注|成员|来源|金额|用户|包含|所有|大于|小于|超过|以下|以上", literal[1])):
            names = [v for v in names if v not in DIRECTION_LABELS or v in literal_categories]
            if not re.search(r"沿用|仍按|还是按", question) and set(names) != set(filters.excludeCategories):
                issues.append({"type": "unsupported_user_condition", "tool": "aggregate", "field": "excludeCategories"})
                if strip:
                    filters.excludeCategories = list(dict.fromkeys(names))
    if exclude.search(question) or re.search(r"只(?:看|统计|算)|仅(?:限|看|统计)", question):
        positive_text = exclude.sub("", question)
        inherited = set()
        if re.search(r"沿用|仍按|还是按|保留.{0,6}筛选", question) and not re.search(r"只看|只统计|仅限", question):
            for row in prior.get("conversation", {}).get("analyses", []):
                if row.get("tool") == "aggregate":
                    inherited.update((row.get("aggregate") or {}).get("filters", {}).get("includeCategories", []))
        allowed = [name for name in filters.includeCategories
                   if name not in filters.excludeCategories and (name in positive_text or name in inherited)]
        if allowed != filters.includeCategories:
            issues.append({"type": "unsupported_user_condition", "tool": "aggregate", "field": "includeCategories"})
            if strip:
                filters.includeCategories = allowed

    # A directional aggregate ignores opposite-signed entries, including them in
    # neither its transaction count nor its average. Mixed requests keep each task's metric.
    if single_aggregate and direction and task.metric != direction:
        issues.append({"type": "unsupported_user_condition", "tool": "aggregate", "field": "metric"})
        if strip:
            task.metric = direction
    return issues


def _check_conditions(analyses, plan, question, mode, previous, *, strip):
    issues = []
    literal_categories, direction = _direction_request(question)
    scope = {key[:3] for key in _scope_for(None, plan, mode)}
    structured = [t.tool for t in plan.analyses if t.tool in DECISION_TOOLS or t.tool == 'aggregate']
    if (strip and direction and len(scope) == 1 and len(structured) == len(set(structured))
            and not plan._analysis_context.get('ambiguous')):
        plan.output.metric = direction
    forecast_request = any(_direction_request(clause)[1] == direction and
                           re.search(r"预测|预计|预估|预期|未来|接下来|下下?个?月|明年", clause)
                           and not re.search(r"(?:别|不必|不用|无需|不要)(?:再)?(?:预测|预估|考虑)", clause)
                           for clause in CLAUSES.split(question)) if direction else False
    single_forecast = (forecast_request and len(scope) == 1 and not plan._analysis_context.get('ambiguous')
                       and [name for name in structured if name in DECISION_TOOLS] == ['forecast']
                       and not re.search(r"两种情景|各个情景|不同情景|分别假设", question))
    # resolve_group shares horizon fields across decision tools on one logical
    # query. Check populated sibling horizons before they enter savings context.
    savings_scopes = {frozenset(_scope_for(t, plan, mode)) for t in analyses if t.tool == "savings"}
    durations, _ = _duration_evidence(question)
    retract_deadline = _retracts_deadline(question)
    for task in analyses or [None]:
        prior = _prior_for(task, plan, mode, previous)
        if (strip and retract_deadline and prior.get("months") is not None
                and prior["months"] not in durations):
            issues.append({"type": "withdrawn_deadline", "tool": task.tool if task else "context",
                           "field": "months", "retirePrevious": True})
        if (strip and task and task.tool in DECISION_TOOLS and (task.tool == "savings" or
                ((task.months is not None or task.monthsDelta is not None)
                 and (retract_deadline or frozenset(_scope_for(task, plan, mode)) in savings_scopes)))):
            allowed = _supported_horizons(question, prior, durations)
            if task.months not in allowed:
                corrected = next(iter(allowed)) if len(allowed) == 1 else None
                if corrected != task.months:
                    if task.months is not None:
                        issues.append({"type": "normalized_savings_horizon", "tool": task.tool, "field": "months",
                                       "previousValue": task.months, "value": corrected})
                    task.months = corrected
            if task.monthsDelta is not None and (task.monthsDelta != explicit_month_delta(question) or not prior.get("months")):
                issues.append({"type": "unsupported_user_condition", "tool": task.tool, "field": "monthsDelta"})
                task.monthsDelta = None
        if task and task.tool == "forecast" and single_forecast and task.metric != direction:
            issues.append({"type": "unsupported_user_condition", "tool": "forecast", "field": "metric"})
            if strip:
                task.metric = direction
        if task and task.tool == "aggregate":
            issues.extend(_aggregate_constraints(task, plan, question, prior, mode, strip=strip,
                literal_categories=literal_categories, direction=direction,
                single_aggregate=sum(t.tool == "aggregate" for t in plan.analyses) <= 1
                and sum(t.tool == "aggregate" for t in analyses) <= 1))
        for field in ROLES:
            value = getattr(task, field, None)
            retire = strip and _retracts(question, field, prior.get(field))
            unsupported = value is not None and not _amount_supported(question, field, value, prior)
            if retire or unsupported:
                issues.append({"type": "unsupported_user_condition", "tool": task.tool if task else "context",
                               "field": field, **({"retirePrevious": True} if retire else {})})
                if strip and unsupported:
                    setattr(task, field, None)
        withdrawn = _withdrawn_changes(question, prior.get("changes", []))
        if strip:
            for metric, source in sorted(withdrawn):
                issues.append({"type": "withdrawn_source_assumption", "field": "changes", "metric": metric, "source": source})
        if not task:
            continue
        kept = [change for change in task.changes if _change_supported(change, question, prior, withdrawn)]
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


# Consume all units in one duration before interpreting it. A calendar year
# such as 2027 exceeds the supported decision horizon and is discarded whole.
_DURATION = re.compile(
    rf"(?<![\d.零〇一二两三四五六七八九十百千万亿])"
    rf"(?:(?P<years>{NUMBER})\s*年(?:\s*(?:又|零)?\s*(?:(?P<half>半)|(?P<extra>{NUMBER})\s*个?月))?"
    rf"|(?P<halfyear>半年)|(?P<months>{NUMBER})\s*个\s*月)")


def _duration_evidence(question):
    durations, offsets = {"future": set(), "unspecified": set()}, set()
    for clause in CLAUSES.split(question):
        retractions = _deadline_retractions(clause)
        for match in _DURATION.finditer(clause):
            # A duration inside a cancellation names the old deadline. The
            # duration immediately before “期限取消” serves the same purpose.
            if any((match.start() < retract.end() and retract.start() < match.end()) or
                   (match.end() <= retract.start() and re.fullmatch(r"\s*的?\s*", clause[match.end():retract.start()]))
                   for retract in retractions):
                continue
            role = _temporal_role(clause, match)
            before, after = clause[:match.start()], clause[match.end():].lstrip()
            if (role == "history" or after.startswith(("前", "以前")) or re.search(r"每(?:隔)?\s*$|(?:过往|此前|之前|前|上)\s*$", before)
                    or re.match(r"(?:的(?:收支|收入|支出|账单))?(?:作为|作|为)?(?:参考|参照|样本)", after)):
                continue
            if match["years"]:
                years = _number(match["years"])
                extra = 6 if match["half"] else _number(match["extra"] or "0")
                if years is None or extra is None:
                    continue
                count = 12 * years + extra
            else:
                count = 6 if match["halfyear"] else _number(match["months"])
            if count is not None and 1 <= count <= 600 and int(count) == count:
                durations[role].add(int(count))
                if after.startswith("后"):
                    offsets.add(int(count))
    return durations["future"] or durations["unspecified"], offsets


def _supported_horizons(question, prior, durations):
    if not durations and _retracts_deadline(question):
        return set()
    delta = explicit_month_delta(question)
    if delta:
        revised = prior["months"] + delta if prior.get("months") else None
        return {revised} if revised is not None and 1 <= revised <= 600 else set()
    return durations or ({prior["months"]} if prior.get("months") else set())


def _time_evidence(question, today):
    durations, offsets = _duration_evidence(question)
    targets = {shift_month(today, offset).strftime("%Y-%m") for offset in offsets}
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
    return targets, durations


def explicit_projection_start(question, today):
    months = rf"(?:\d{{4}}[-/.]\d{{1,2}}|(?:(?:\d{{4}}年|明年|今年)\s*)?{NUMBER}月|下下?个?月|本月|这个月)"
    starts = set()
    for clause in CLAUSES.split(question):
        if NEGATED.search(clause) or HISTORY_WORDS.search(clause):
            continue
        for match in re.finditer(rf"(?:从)?({months})\s*(?:份)?\s*(?:开始|起算|起)", clause):
            targets, _ = _time_evidence(match[1], today)
            starts.update(t for t in targets if t >= today.strftime("%Y-%m"))
    return next(iter(starts)) if len(starts) == 1 else None


def _confirmed_amount_bounds(question, inherited):
    bounds = {field: inherited[field] for field in ("minAmount", "maxAmount") if inherited.get(field) is not None}
    operators = {
        "minAmount": ("下限|最低金额", "不少于|不低于|至少|大于等于|>=|≥", "以上|及以上|起"),
        "maxAmount": ("上限|最高金额", "不超过|不高于|至多|最多|小于等于|<=|≤", "以下|及以下|以内"),
    }
    cancel = r"(?:取消|撤销|去掉|删除|移除|放开|不再设|不设)(?:原有的?|原来的?)?"
    for clause in CLAUSES.split(question):
        if re.search(cancel + r"(?:全部)?金额(?:限制|筛选|过滤)|金额(?:不限|不设限)|不再按金额(?:筛选|过滤)", clause):
            bounds.clear()
        for field, (label, comparison, suffix) in operators.items():
            if re.search(cancel + rf"(?:金额)?(?:{label})|(?:{label})(?:取消|不限)", clause):
                bounds.pop(field, None)
                continue
            previous = rf"(?:从|由)\s*{NUMBER}\s*(?:元|块)?\s*"
            update = r"(?:(?:调整|改|调|提高|降低|上调|下调|设)(?:为|到|至|成)|为|是)"
            after = rf"(?:{comparison}|(?:{label})(?:{previous})?{update}?)\s*[￥¥]?\s*({NUMBER})"
            before = rf"({NUMBER})\s*(?:元|块)?\s*(?:{suffix})"
            values = {_number(raw) for raw in re.findall(after, clause) + re.findall(before, clause)}
            if len(values) == 1:
                bounds[field] = values.pop()
    return bounds


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
        # A replacement must retain a user's explicit selection on the same
        # logical query even when the review simply omits the filter.
        prior = _prior_for(task, plan, mode, previous)
        if task.tool == "aggregate":
            matching = [old for old in original if old.aggregate and _scope_for(old, plan, mode) == scope]
            if len(matching) == 1:
                required = matching[0].aggregate.filters
                inherited = _referenced_aggregate_filters(question, prior, task)
                bounds = _confirmed_amount_bounds(question, inherited)
                for field, limit in bounds.items():
                    # Only protect a bound verified for this logical query. Another
                    # query in the same comparison may have a different threshold.
                    old = getattr(required, field)
                    if limit == 0 and field == "minAmount":
                        continue
                    belongs_to_query = limit in (old, inherited.get(field)) or (
                        len(all_scopes) == 1 and (old is not None or field in inherited))
                    if belongs_to_query and (not task.aggregate or getattr(task.aggregate.filters, field) != limit):
                        return "unsupported_" + field
                constrained = re.search(r"只看|只统计|仅(?:看|限|统计)|排除|剔除|不含|不包括", question)
                for field in _FILTER_DIMENSIONS if constrained else ():
                    old = getattr(required, field)
                    if not old:
                        continue
                    values = [old] if isinstance(old, str) else old
                    named = {value for value in values if value in question and (
                        value.casefold() not in _UNRESTRICTED or _placeholder_is_literal(value, _FILTER_DIMENSIONS[field], question))}
                    new = getattr(task.aggregate.filters, field) if task.aggregate else []
                    if named - ({new} if isinstance(new, str) else set(new)):
                        return "unsupported_" + field
        prior_target = prior.get("budgetTargetMonth" if task.tool == "budget" else "targetMonth")
        target = task.targetMonth
        if target:
            if task.tool in {"forecast", "projection"} and target < today.strftime("%Y-%m"):
                return "unsupported_targetMonth"
            defaults = {shift_month(today, 1).strftime("%Y-%m")} if task.tool in {"forecast", "projection"} else set()
            valid = targets or ({prior_target} if prior_target else defaults)
            if target not in valid:
                return "unsupported_targetMonth"
        delta = explicit_month_delta(question)
        if task.monthsDelta is not None and (delta != task.monthsDelta or not prior.get("months")):
            return "unsupported_monthsDelta"
        horizons = _supported_horizons(question, prior, durations)
        if task.tool in DECISION_TOOLS and task.months is not None and task.months not in horizons:
            return "unsupported_months"
    return None


def _execution_tasks(tasks, plan, mode):
    records = []
    for task in tasks:
        fields = task.model_dump(mode="json", exclude={"queryIds"})
        for change in fields["changes"]:
            change.pop("label", None)
        if task.tool not in DECISION_TOOLS:
            for ignored in ("months", "monthsDelta", "targetMonth"):
                fields.pop(ignored, None)
        # Query IDs may differ while resolving to identical authorized data.
        fields["scope"] = sorted(map(str, _scope_for(task, plan, mode)))
        records.append(json.dumps(fields, sort_keys=True))
    return sorted(records)


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
                   "calendarContext": calendar_context(today), "previousContext": to_jsonable_python(compact_user_context(previous)),
                   "logicalQueryIds": sorted(logical_ids),
                   "plan": plan.model_dump(mode="json", exclude_none=True, exclude_defaults=True)}
        system = PROMPT + json.dumps(ANALYSIS_TOOLS, ensure_ascii=False)
        if get_settings().llm_provider == 'openai_compatible':
            system += "\nJSON schema：" + json.dumps(PlanReviewDecision.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
        attempts = []
        audit = {"status": "unavailable", "attempts": attempts}
        for attempt in range(2):
            started = perf_counter()
            call = {}
            attempts.append(call)
            try:
                bounded, _, budget = build_report_context(payload, [], system, {},
                    num_ctx=get_settings().ollama_num_ctx, num_predict=get_settings().planner_num_predict,
                    include_values=False)
                call['contextBudget'] = budget
                response = await asyncio.wait_for(get_structured_model(PlanReviewDecision).ainvoke([
                    ("system", system), ("human", json.dumps(bounded, ensure_ascii=False, separators=(',', ':')))]),
                    timeout=get_settings().planner_timeout_seconds)
                call.update(_call_metadata(response))
                decision = _parse(response)
                issue = _binding_issue(decision, plan, logical_ids, question, today, book_mode, previous)
                if issue:
                    return None, {**audit, "error": issue}
                if decision.adequate and (decision.analysisWindow != plan.analysisWindow or
                        _execution_tasks(decision.analyses, plan, book_mode) != _execution_tasks(plan.analyses, plan, book_mode)):
                    # The supplied execution diff has already passed all scope and
                    # user-condition checks. Its existence proves a correction is
                    # needed, regardless of the model's contradictory summary flag.
                    decision.adequate = False
                    audit['adequacyNormalized'] = True
                    call['reportedAdequate'] = True
                return decision, {**audit, "status": "reviewed", "decision": decision.model_dump(mode="json")}
            except ValidationError as exc:
                call["error"] = "invalid_structure"
                errors = exc.errors(include_input=False, include_context=False, include_url=False)
                if attempt:
                    return None, {**audit, "error": "invalid_structure"}
                payload["corrections"] = [{"field": ".".join(map(str, e["loc"]))[:100], "type": e["type"][:60]}
                                          for e in errors[:5]]
            except Exception as exc:
                call["error"] = type(exc).__name__
                return None, {**audit, "error": type(exc).__name__}
            finally:
                call["durationMs"] = round((perf_counter() - started) * 1000)
