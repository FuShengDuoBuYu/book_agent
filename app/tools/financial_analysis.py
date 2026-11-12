"""Deterministic evidence for open-ended ledger analysis.

The planner chooses tools. Amounts, comparisons and scenarios are calculated here
so the answering model can explain evidence without doing ledger arithmetic.
"""

import json
import math
import re
from calendar import monthrange
from collections import Counter, defaultdict
from datetime import date
from statistics import median, quantiles

from app.tools.order_calculations import (
    calculate_category_breakdown,
    calculate_order_summary,
    calculate_payment_method_breakdown,
    calculate_remark_breakdown,
    _compact_order,
    _round_money,
)


ANALYSIS_TOOLS = {
    "cashflow": "收入、支出、净现金流、笔数和分类结构",
    "trends": "按月和按日的趋势，区分本月未结束与完整月份",
    "changes": "最近两个完整月份收支变化及各类别贡献，解释变化来自哪里",
    "anomalies": "同类大额支出和疑似重复记账，仅提供待核查线索",
    "recurring": "重复出现的收入来源、周期性支出及金额稳定程度",
    "forecast": "预测指定月份的 income / expense / net，返回基准与历史情景区间",
    "scenario": "在历史月度基准上计算用户给定的收入或支出变化及净结余",
    "budget": "根据用户提供的 budgetAmount 计算目标月份剩余预算、超支及剩余每日额度",
    "savings": "根据用户提供的 startingBalance、targetAmount 和 months 计算储蓄目标进度及所需月结余",
    "projection": "未来多个月的收入、支出与累计新增结余，支持来源按比例变化，不需要当前存款余额",
    "income_stability": "历史收入来源占比、波动与连续性，仅评价已观察的月份",
    "optimization": "在保留用户 protectedCategories 和必要生活支出的前提下，分析 monthlyImprovement 的节支空间",
    "purchase": "购买 purchaseAmount 对现金余额和 reserveAmount 的影响；缺少 startingBalance 时不能判断买得起",
    "runway": "用户给定收入变化后，现有 startingBalance 能覆盖多久的现金缺口",
    "data_quality": "退款、转入、一次性入账、缺失字段及重复线索对分析的影响",
}


def shift_month(value: date, count: int) -> date:
    index = value.year * 12 + value.month - 1 + count
    year, month = divmod(index, 12)
    return date(year, month + 1, 1)


def merge_orders(datasets: list[list[dict]]) -> list[dict]:
    """Remove overlap between queries while preserving duplicate ledger rows."""
    multiplicity = Counter()
    records = {}
    for orders in datasets:
        counts = Counter()
        for order in orders:
            key = json.dumps(order, sort_keys=True, ensure_ascii=False, default=str)
            counts[key] += 1
            records[key] = order
        multiplicity |= counts
    return [records[key] for key, count in multiplicity.items() for _ in range(count)]


def _date(order: dict) -> date | None:
    try:
        return date(int(order["year"]), int(order["month"]), int(order["day"]))
    except (KeyError, TypeError, ValueError):
        return None


def _source(order: dict) -> str:
    category = str(order.get("costType") or "未分类").strip()
    remark = str(order.get("orderRemark") or "").strip()
    if category in {"收入", "其他", "其它", "未分类"} and remark:
        # Ignore dates and amounts in a description when finding repeated sources.
        remark = re.sub(r"\d+(?:[./年\-]\d+)*(?:月|日|元)?", "", remark)
        remark = re.sub(r"\s+", " ", remark).strip()
        return remark[:48] or category
    return category


def _nonrecurring_income(order: dict) -> bool:
    text = f"{order.get('costType', '')} {order.get('orderRemark', '')}"
    if any(word in text for word in ("退款", "退货", "报销", "借款", "借入", "归还借款", "出售", "卖车", "旧车", "二手", "一次性", "年终奖")):
        return True
    earned = ("工资", "薪", "家教", "课时", "授课", "劳务", "兼职", "稿费", "租金", "助研", "助教", "补贴")
    if any(word in text for word in earned):
        return False
    return any(word in text for word in ("转入", "转账", "存款", "借还", "红包", "礼金", "报账"))


def _totals(orders: list[dict]) -> dict:
    return json.loads(calculate_order_summary(json.dumps({"ordersInfo": orders})))


def _buckets(orders: list[dict], *, daily: bool = False) -> dict:
    buckets = defaultdict(list)
    for order in orders:
        key = _date(order).isoformat()
        buckets[key if daily else key[:7]].append(order)
    return dict(sorted(buckets.items()))


def _series(orders: list[dict], as_of: date, window_start: date | None = None, window_end: date | None = None) -> list[dict]:
    current = as_of.strftime("%Y-%m")
    rows = []
    for month, items in _buckets(orders).items():
        start = date.fromisoformat(month + "-01")
        end = date(start.year, start.month, monthrange(start.year, start.month)[1])
        partial = month == current or bool(window_start and start < window_start) or bool(window_end and end > window_end)
        totals = _totals(items)
        rows.append({"month": month, "orderCount": len(items), "incomeTotal": totals["incomeTotal"],
                     "expenseTotal": totals["expenseTotal"], "netIncomeMinusExpense": totals["netIncomeMinusExpense"],
                     "netIncomeRatioPercent": _round_money(totals["netIncomeMinusExpense"] / totals["incomeTotal"] * 100) if totals["incomeTotal"] else None,
                     "partial": partial})
    return rows


def _change(before: dict, after: dict) -> dict:
    result = {"from": before["month"], "to": after["month"]}
    for name, field in (("income", "incomeTotal"), ("expense", "expenseTotal"), ("net", "netIncomeMinusExpense")):
        change = _round_money(after[field] - before[field])
        result[name + "Change"] = change
        result[name + "ChangePercent"] = _round_money(change / abs(before[field]) * 100) if before[field] else None
    return result


def _cashflow(orders: list[dict], *, as_of: date, **kwargs) -> dict:
    payload = json.dumps({"ordersInfo": orders}, ensure_ascii=False)
    summary = _totals(orders)
    sources = defaultdict(float)
    for order in orders:
        if float(order["money"]) > 0:
            sources[_source(order)] += float(order["money"])
    income = summary["incomeTotal"]
    complete = [row for row in _series(orders, as_of, kwargs.get("window_start"), kwargs.get("window_end")) if not row["partial"]]
    averages = {name: _round_money(sum(row[field] for row in complete) / len(complete)) if complete else None
                for name, field in (("income", "incomeTotal"), ("expense", "expenseTotal"), ("net", "netIncomeMinusExpense"))}
    averages["netIncomeRatioPercent"] = _round_money(averages["net"] / averages["income"] * 100) if averages["income"] else None
    categories = json.loads(calculate_category_breakdown(payload))
    for row in categories["expenseCategories"]:
        row["shareOfIncomePercent"] = _round_money(row["amount"] / income * 100) if income else None
    return {**summary, **categories,
            **json.loads(calculate_payment_method_breakdown(payload)),
            **json.loads(calculate_remark_breakdown(payload)),
            "expenseIncomeRatioPercent": _round_money(summary["expenseTotal"] / income * 100) if income else None,
            "netIncomeRatioPercent": _round_money(summary["netIncomeMinusExpense"] / income * 100) if income else None,
            "incomeSources": [{"source": name, "amount": _round_money(amount),
                               "sharePercent": _round_money(amount / income * 100) if income else None}
                              for name, amount in sorted(sources.items(), key=lambda item: item[1], reverse=True)][:20],
            "completeMonthAverage": {**averages, "months": [row["month"] for row in complete]},
            "computedConclusion": (f"完整月份共 {len(complete)} 个，月均收入 {averages['income']} 元，月均支出 {averages['expense']} 元，月均结余 {averages['net']} 元，完整月份结余率 {averages['netIncomeRatioPercent']}%。这组均值包含完整月份中的大额支出，不能用常态或中位数替换。" if complete else "缺少可计算月均值的完整月份。"),
            "interpretation": "支出占比说明结构，shareOfIncomePercent 才是该支出占收入的比例。资产与债务余额未知，不能据此判断抗风险能力或偿付压力。",
            "averageDefinition": "averageIncome 与 averageExpense 是每笔交易均值；completeMonthAverage 不含未完月份，外层比例为整个查询期累计比例。"}


def _trends(orders: list[dict], *, as_of: date, **kwargs) -> dict:
    months = _series(orders, as_of, kwargs.get("window_start"), kwargs.get("window_end"))
    complete = [row for row in months if not row["partial"]]
    return {
        "months": months,
        "days": [{"date": day, **{key: value for key, value in _totals(items).items()
                                   if key in {"incomeTotal", "expenseTotal", "netIncomeMinusExpense", "orderCount"}}}
                 for day, items in _buckets(orders, daily=True).items()][-90:],
        "latestCompleteChange": _change(*complete[-2:]) if len(complete) >= 2 else None,
        "coverageNote": f"{as_of:%Y-%m} 尚未结束，不能得出支出已恢复正常、回落、下降或整月模式稳定的结论。未出现账单的月份代表缺少记录，不能确认实际收支为零。",
    }


def _changes(orders: list[dict], *, as_of: date, **kwargs) -> dict:
    complete = [row for row in _series(orders, as_of, kwargs.get("window_start"), kwargs.get("window_end")) if not row["partial"]]
    if len(complete) < 2:
        return {"sufficientData": False, "reason": "需要至少两个完整自然月的账单记录。"}
    before, after = complete[-2:]
    buckets = _buckets(orders)
    result = {"sufficientData": True, "before": before, "after": after, **_change(before, after),
              "interpretation": f"变化只比较 {before['month']} 与 {after['month']} 两个完整月份。{as_of:%Y-%m} 尚未结束，无法据此断言趋势已恢复或未来将回落。"}
    for direction, positive in (("income", True), ("expense", False)):
        amounts = []
        for month in (before["month"], after["month"]):
            groups = defaultdict(float)
            for order in buckets[month]:
                money = float(order["money"])
                if (money > 0) == positive and money != 0:
                    groups[_source(order)] += abs(money)
            amounts.append(groups)
        result[direction + "Drivers"] = sorted([
            {"name": name, "before": _round_money(amounts[0][name]),
             "after": _round_money(amounts[1][name]),
             "change": _round_money(amounts[1][name] - amounts[0][name]),
             "afterRemarks": list(dict.fromkeys(str(row.get("orderRemark") or "") for row in buckets[after["month"]]
                                                if _source(row) == name and (float(row["money"]) > 0) == positive))[:5]}
            for name in amounts[0].keys() | amounts[1].keys()
        ], key=lambda row: abs(row["change"]), reverse=True)[:20]
    return result


def _anomalies(orders: list[dict], **kwargs) -> dict:
    categories = defaultdict(list)
    duplicates = defaultdict(list)
    for order in orders:
        money = float(order["money"])
        if money < 0:
            categories[str(order.get("costType") or "未分类")].append(order)
        fingerprint = (_date(order), order.get("clock"), money, order.get("costType"), order.get("orderRemark"), order.get("bankName"))
        duplicates[fingerprint].append(order)
    large = []
    insufficient = []
    for category, items in categories.items():
        values = [abs(float(item["money"])) for item in items]
        if len(values) < 4:
            insufficient.append({"category": category, "count": len(values)})
            continue
        q1, _, q3 = quantiles(values, n=4, method="inclusive")
        threshold = max(q3 + 1.5 * (q3 - q1), median(values) * 3)
        for item in items:
            if abs(float(item["money"])) > threshold:
                large.append({**_compact_order(item), "category": category, "threshold": _round_money(threshold)})
    return {
        "largeExpenses": sorted(large, key=lambda item: item["amount"], reverse=True)[:15],
        "possibleDuplicates": [
            {"example": _compact_order(items[0]), "count": len(items)}
            for items in duplicates.values() if len(items) > 1
        ][:15],
        "insufficientCategories": insufficient,
        "rule": "同类至少四笔，金额超过 max(Q3 + 1.5×IQR, 中位数×3)。重复项按日期、时间、金额、分类、备注、支付方式匹配。",
        "interpretation": "这些是核查线索，真实大额消费和重复购买也可能满足条件。空结果只说明规则未命中，不证明账单正常、完整、没有非计划支出或交易标识唯一。样本少于四笔的类别无法进行同类异常判断。",
    }


def _recurring(orders: list[dict], *, as_of: date, **kwargs) -> dict:
    groups = defaultdict(list)
    for order in orders:
        direction = "income" if float(order["money"]) > 0 else "expense"
        groups[(direction, _source(order))].append(order)
    result = {"income": [], "expense": []}
    for (direction, source), items in groups.items():
        month_totals = {month: sum(abs(float(row["money"])) for row in rows) for month, rows in _buckets(items).items()}
        if len(month_totals) < 2:
            continue
        dates = sorted({_date(item) for item in items})
        gaps = [(later - earlier).days for earlier, later in zip(dates, dates[1:])]
        partial = [row["month"] for row in _series(items, as_of, kwargs.get("window_start"), kwargs.get("window_end")) if row["partial"]]
        full_month_amounts = [amount for month, amount in month_totals.items() if month not in partial]
        result[direction].append({
            "source": source, "months": month_totals,
            "typicalAmount": _round_money(median(full_month_amounts)) if full_month_amounts else None,
            "partialMonths": partial,
            "activeMonths": len(month_totals), "transactions": len(items),
            "medianGapDays": median(gaps) if gaps else None,
            "possibleNonrecurring": direction == "income" and any(_nonrecurring_income(item) for item in items),
        })
    for rows in result.values():
        rows.sort(key=lambda row: row["typicalAmount"] or 0, reverse=True)
        del rows[20:]
    return result


def _forecast(orders: list[dict], *, as_of: date, metric: str = "income", target_month: str | None = None, **kwargs) -> dict:
    target = date.fromisoformat(target_month + "-01") if target_month else shift_month(as_of, 1)
    cutoff = min(as_of.replace(day=1), target)
    start = shift_month(cutoff, -6)
    history = [order for order in orders if start <= _date(order) < cutoff]
    buckets = _buckets(history)
    values = []
    excluded = 0.0
    for month, items in buckets.items():
        earned = [order for order in items if not (float(order["money"]) > 0 and _nonrecurring_income(order))]
        totals = _totals(earned)
        excluded += sum(float(order["money"]) for order in items if float(order["money"]) > 0 and _nonrecurring_income(order))
        values.append({"month": month, "income": totals["incomeTotal"], "expense": totals["expenseTotal"], "net": totals["netIncomeMinusExpense"]})
    recent = values[-3:]
    samples = [row[metric] for row in recent]
    sufficient = len(samples) >= 2
    estimate = None
    if sufficient:
        estimate = median(samples)
        if metric == "net":
            estimate = median(row["income"] for row in recent) - median(row["expense"] for row in recent)
    expected = [shift_month(start, offset).strftime("%Y-%m") for offset in range(6)]
    missing = [month for month in expected if month not in buckets]
    return {
        "metric": metric, "targetMonth": target.strftime("%Y-%m"),
        "sufficientData": sufficient,
        "estimate": _round_money(estimate) if sufficient else None,
        "lower": min(samples) if sufficient else None,
        "upper": max(samples) if sufficient else None,
        "referenceValue": samples[-1] if samples else None,
        "observedMonths": list(buckets), "missingMonths": missing,
        "baselineMonths": [row["month"] for row in recent], "monthlyEvidence": values,
        "excludedIncomeTotal": _round_money(excluded),
        "sources": _recurring(history, as_of=as_of),
        "latestCompleteChange": _changes(history, as_of=as_of),
        "method": "最近六个完整自然月内，取最近三个有账单月份的收入和支出中位数作为月度基准，结余为两者之差，最小值和最大值作为历史情景范围。",
        "limitations": [
            "历史情景范围不表示统计置信区间或收入保证。",
            "账单空白月份未按零处理，账本完整性需要用户确认。",
            "显式退款、报销、借款、资产出售、一次性入账与内部转入从预测收入剔除，备注识别可能有误。",
            "岗位、课时、季节、节假日及未来大额支出变化需要用户补充。",
        ],
    }


def _scenario(orders: list[dict], *, as_of: date, target_month=None, changes=None, **kwargs) -> dict:
    income = _forecast(orders, as_of=as_of, metric="income", target_month=target_month)
    expense = _forecast(orders, as_of=as_of, metric="expense", target_month=target_month)
    changes = changes or []
    if not income["sufficientData"] or not changes:
        return {"sufficientData": False, "baseline": income, "reason": "需要历史月度基准和用户明确给出的变化金额或次数。"}
    from app.tools.decision_analysis import apply_changes
    adjusted = apply_changes(orders, income, expense, changes)
    if not adjusted["sufficientData"]:
        return adjusted
    totals, assumptions = adjusted["totals"], adjusted["assumptions"]
    net = _round_money(totals["income"] - totals["expense"])
    baseline_net = _round_money(income["estimate"] - expense["estimate"])
    ratio = _round_money(net / totals["income"] * 100) if totals["income"] else None
    return {"sufficientData": True, **totals, "net": net,
            "baselineNet": baseline_net, "netChange": _round_money(net - baseline_net), "netIncomeRatioPercent": ratio,
            "computedConclusion": f"在用户假设实现且其他收支延续历史基准的条件下，收入 {totals['income']} 元，支出 {totals['expense']} 元，结余 {net} 元，结余占收入 {ratio}%。基准结余 {baseline_net} 元，改善 {_round_money(net - baseline_net)} 元。这不证明真实资产或抗风险能力。",
            "assumptions": assumptions, "baseline": {"income": income, "expense": expense}}


def _budget(orders: list[dict], *, as_of: date, target_month=None, budget_amount=None, **kwargs) -> dict:
    if budget_amount is None:
        return {"sufficientData": False, "missingInputs": ["budgetAmount"]}
    target = date.fromisoformat(target_month + "-01") if target_month else as_of.replace(day=1)
    items = [order for order in orders if _date(order).replace(day=1) == target]
    summary = _totals(items)
    spent = summary["expenseTotal"]
    days = monthrange(target.year, target.month)[1]
    if target == as_of.replace(day=1):
        days -= as_of.day - 1
    elif target < as_of.replace(day=1):
        days = 0
    remaining = _round_money(budget_amount - spent)
    return {"sufficientData": True, "month": target.strftime("%Y-%m"), "budgetAmount": budget_amount,
            "expenseOrderCount": summary["expenseOrderCount"],
            "spentPercent": _round_money(spent / budget_amount * 100) if budget_amount else None,
            "spent": spent, "remaining": remaining, "overBudget": max(0, -remaining),
            "remainingDaysIncludingToday": days,
            "dailyAllowance": _round_money(max(0, remaining) / days) if days else None,
            "hasRecords": bool(items), "assumption": "预算金额来自用户，支出仅覆盖账本已记录的数据。"}


def _savings(orders: list[dict], *, as_of: date, starting_balance=None, target_amount=None, months=None, **kwargs) -> dict:
    missing = [name for name, value in (("startingBalance", starting_balance), ("targetAmount", target_amount)) if value is None]
    baseline = _forecast(orders, as_of=as_of, metric="net")
    if kwargs.get("changes"):
        changed = _scenario(orders, as_of=as_of, changes=kwargs["changes"])
        if not changed["sufficientData"]:
            return changed
        baseline = {**baseline, "estimate": changed["net"], "assumptions": changed["assumptions"]}
    if missing or not baseline["sufficientData"]:
        return {"sufficientData": False, "missingInputs": missing, "baseline": baseline}
    remaining = max(0, _round_money(target_amount - starting_balance))
    monthly = baseline["estimate"]
    months_to_goal = None
    if remaining == 0:
        months_to_goal = 0
    elif monthly > 0:
        months_to_goal = math.ceil(remaining / monthly)
    projected = _round_money(starting_balance + monthly * months) if months else None
    return {"sufficientData": True, "startingBalance": starting_balance, "targetAmount": target_amount,
            "remainingToTarget": remaining, "baselineMonthlySaving": monthly,
            "estimatedMonthsToTarget": months_to_goal, "months": months,
            "requiredMonthlySaving": _round_money(remaining / months) if months else None,
            "projectedBalance": projected,
            "computedConclusion": (f"仅在后续净现金流延续历史基准的假设下，{months} 个月后预计余额 {projected} 元，{'达到' if projected >= target_amount else '未达到'}目标 {target_amount} 元；未来收支变化可能改变结果，不能保证达成。" if months else "需要结合未来收支变化判断目标期限，历史基准不能保证未来结果。"),
            "baseline": baseline, "assumption": "当前余额、目标和期限来自用户，后续净现金流假设延续历史模式。"}


_IMPLEMENTATIONS = {
    "cashflow": _cashflow, "trends": _trends, "changes": _changes,
    "anomalies": _anomalies, "recurring": _recurring, "forecast": _forecast, "scenario": _scenario,
    "budget": _budget, "savings": _savings,
}


def run_analysis(name: str, orders: list[dict], *, as_of: date, **kwargs) -> dict:
    valid = []
    for order in orders:
        when = _date(order)
        try:
            amount = float(order.get("money", 0))
        except (TypeError, ValueError):
            continue
        if when and when <= as_of and math.isfinite(amount):
            valid.append(order)
    implementation = _IMPLEMENTATIONS.get(name)
    if implementation is None:
        from app.tools.decision_analysis import DECISION_IMPLEMENTATIONS
        implementation = DECISION_IMPLEMENTATIONS[name]
    result = implementation(valid, as_of=as_of, **kwargs)
    return {**result, "asOf": as_of.isoformat(), "excludedInvalidOrFutureRows": len(orders) - len(valid)}
